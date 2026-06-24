// license:GPLv3+
//
// VPX Bot Bridge plugin (Milestone 1)
// ------------------------------------
// Bridges a running VPX table to an external bot process:
//   - Telemetry OUT: every physics update (OnUpdatePhysics, ~1 kHz best-effort)
//     it snapshots ball + flipper state and streams it as one NDJSON line over
//     a loopback TCP socket.
//   - Commands IN: NDJSON command lines ("flip_left"/"flip_right" with a
//     press flag) are parsed on a worker thread and applied on the physics
//     thread via the sanctioned VPXPluginAPI::SetActionState.
//
// Threading model (matches the engine's constraints — see FINDINGS.md):
//   * The MsgPlugin bus and VPX API are NOT thread-safe; everything that
//     touches them runs on the physics/main thread inside the OnUpdatePhysics
//     callback.
//   * All blocking socket I/O runs on dedicated worker threads. The physics
//     callback only does a cheap snapshot copy + condition-variable notify
//     (telemetry) and reads atomics (commands).
//
// Transport for M1 is loopback TCP + NDJSON: simplest thing that is
// non-blocking on the physics thread, language-neutral, and lets us measure
// round-trip latency. Binary/shared-memory is the documented upgrade path.

#include "plugins/MsgPlugin.h"
#include "plugins/VPXPlugin.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <atomic>
#include <mutex>
#include <condition_variable>
#include <thread>
#include <chrono>

#ifdef _WIN32
   #include <winsock2.h>
   #include <ws2tcpip.h>
   #pragma comment(lib, "ws2_32.lib")
   typedef SOCKET bb_socket_t;
   #define BB_INVALID_SOCKET INVALID_SOCKET
   #define BB_CLOSESOCKET closesocket
   #define BB_SHUTDOWN_BOTH SD_BOTH
#else
   #include <sys/socket.h>
   #include <netinet/in.h>
   #include <netinet/tcp.h>
   #include <arpa/inet.h>
   #include <unistd.h>
   typedef int bb_socket_t;
   #define BB_INVALID_SOCKET (-1)
   #define BB_CLOSESOCKET ::close
   #define BB_SHUTDOWN_BOTH SHUT_RDWR
#endif

namespace BotBridge {

// --- configuration -------------------------------------------------------
static const uint16_t kPort = 13501;        // loopback TCP port the bot connects to
static const unsigned int kMaxBalls = 32;
static const unsigned int kMaxFlippers = 16;
static const unsigned int kSchemaVersion = 1;

// --- plugin / message bus state (main thread only) -----------------------
const MsgPluginAPI* msgApi = nullptr;
VPXPluginAPI* vpxApi = nullptr;
uint32_t endpointId = 0;
unsigned int getVpxApiId = 0, onGameStartId = 0, onGameEndId = 0, onUpdatePhysicsId = 0;

// --- telemetry snapshot, published by physics thread, consumed by emitter -
struct Snapshot
{
   uint64_t tick = 0;
   double time = 0.0;
   unsigned int nballs = 0;
   unsigned int nflippers = 0;
   VPXBallState balls[kMaxBalls];
   VPXFlipperState flippers[kMaxFlippers];
};

std::mutex g_txMutex;             // guards g_snap + g_seq
std::condition_variable g_txCv;
Snapshot g_snap;
uint64_t g_seq = 0;
uint64_t g_tick = 0;

// --- commands, set by receiver thread, applied on physics thread ----------
std::atomic<bool> g_flipLeft{ false };
std::atomic<bool> g_flipRight{ false };
bool g_flipLeftApplied = false;
bool g_flipRightApplied = false;

// --- networking ----------------------------------------------------------
std::atomic<bool> g_running{ false };
std::atomic<bool> g_connected{ false };
std::atomic<bb_socket_t> g_listenSock{ BB_INVALID_SOCKET };
std::thread g_serverThread;

// -------------------------------------------------------------------------
// Telemetry serialization (one NDJSON object per physics update).
static std::string serialize(const Snapshot& s)
{
   std::string out;
   out.reserve(128 + s.nballs * 160 + s.nflippers * 32);
   char buf[256];
   snprintf(buf, sizeof(buf), "{\"v\":%u,\"tick\":%llu,\"time\":%.6f,\"balls\":[",
      kSchemaVersion, (unsigned long long)s.tick, s.time);
   out += buf;
   for (unsigned int i = 0; i < s.nballs; ++i)
   {
      const VPXBallState& b = s.balls[i];
      snprintf(buf, sizeof(buf),
         "%s{\"id\":%u,\"x\":%.4f,\"y\":%.4f,\"z\":%.4f,\"vx\":%.5f,\"vy\":%.5f,\"vz\":%.5f,"
         "\"avx\":%.5f,\"avy\":%.5f,\"avz\":%.5f,\"r\":%.3f}",
         i ? "," : "", b.id, b.x, b.y, b.z, b.vx, b.vy, b.vz, b.angVelX, b.angVelY, b.angVelZ, b.radius);
      out += buf;
   }
   out += "],\"flippers\":[";
   for (unsigned int i = 0; i < s.nflippers; ++i)
   {
      snprintf(buf, sizeof(buf), "%s{\"i\":%u,\"angle\":%.4f}", i ? "," : "", i, s.flippers[i].angle);
      out += buf;
   }
   out += "]}\n";
   return out;
}

// Parse the boolean value of a "press" field from a command line.
static bool parsePress(const std::string& line)
{
   size_t p = line.find("press");
   if (p == std::string::npos)
      return false;
   p = line.find(':', p);
   if (p == std::string::npos)
      return false;
   ++p;
   while (p < line.size() && (line[p] == ' ' || line[p] == '\t'))
      ++p;
   return p < line.size() && (line[p] == 't' || line[p] == 'T' || line[p] == '1');
}

// -------------------------------------------------------------------------
// Receiver worker: blocking recv, parse NDJSON command lines into atomics.
static void recvLoop(bb_socket_t client)
{
   std::string buf;
   char tmp[1024];
   while (g_running.load() && g_connected.load())
   {
      const int n = (int)recv(client, tmp, sizeof(tmp), 0);
      if (n <= 0)
      {
         g_connected.store(false);
         break;
      }
      buf.append(tmp, n);
      size_t nl;
      while ((nl = buf.find('\n')) != std::string::npos)
      {
         const std::string line = buf.substr(0, nl);
         buf.erase(0, nl + 1);
         if (line.find("flip_left") != std::string::npos)
            g_flipLeft.store(parsePress(line));
         else if (line.find("flip_right") != std::string::npos)
            g_flipRight.store(parsePress(line));
      }
   }
}

// -------------------------------------------------------------------------
// Server worker: accept one client at a time; emit telemetry; spawn recvLoop.
static void serverLoop()
{
   bb_socket_t listenSock = (bb_socket_t)socket(AF_INET, SOCK_STREAM, 0);
   if (listenSock == BB_INVALID_SOCKET)
      return;
   int one = 1;
   setsockopt(listenSock, SOL_SOCKET, SO_REUSEADDR, (const char*)&one, sizeof(one));

   sockaddr_in addr;
   memset(&addr, 0, sizeof(addr));
   addr.sin_family = AF_INET;
   addr.sin_port = htons(kPort);
   addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK); // 127.0.0.1 only
   if (bind(listenSock, (sockaddr*)&addr, sizeof(addr)) != 0 || listen(listenSock, 1) != 0)
   {
      BB_CLOSESOCKET(listenSock);
      return;
   }
   g_listenSock.store(listenSock);

   while (g_running.load())
   {
      bb_socket_t client = (bb_socket_t)accept(listenSock, nullptr, nullptr);
      if (client == BB_INVALID_SOCKET)
      {
         if (!g_running.load())
            break;
         continue;
      }
      setsockopt(client, IPPROTO_TCP, TCP_NODELAY, (const char*)&one, sizeof(one)); // latency: no Nagle
      g_connected.store(true);
      std::thread rt(recvLoop, client);

      uint64_t lastSeq = 0;
      while (g_running.load() && g_connected.load())
      {
         Snapshot snap;
         {
            std::unique_lock<std::mutex> lk(g_txMutex);
            g_txCv.wait_for(lk, std::chrono::milliseconds(100),
               [&] { return g_seq != lastSeq || !g_running.load() || !g_connected.load(); });
            if (g_seq == lastSeq)
               continue; // timeout / spurious wakeup
            lastSeq = g_seq;
            snap = g_snap; // copy under lock, serialize + send outside
         }
         const std::string line = serialize(snap);
         size_t sent = 0;
         while (sent < line.size() && g_running.load())
         {
            const int n = (int)send(client, line.data() + sent, (int)(line.size() - sent), 0);
            if (n <= 0)
            {
               g_connected.store(false);
               break;
            }
            sent += (size_t)n;
         }
      }

      g_connected.store(false);
      shutdown(client, BB_SHUTDOWN_BOTH); // unblock recvLoop
      if (rt.joinable())
         rt.join();
      BB_CLOSESOCKET(client);
   }

   g_listenSock.store(BB_INVALID_SOCKET);
   BB_CLOSESOCKET(listenSock);
}

// -------------------------------------------------------------------------
// VPX event callbacks (run on the physics / main thread).
void onGameStart(const unsigned int, void*, void*)
{
   g_flipLeftApplied = false;
   g_flipRightApplied = false;
   g_flipLeft.store(false);
   g_flipRight.store(false);
   g_tick = 0;
   if (vpxApi)
      vpxApi->PushNotification("Bot Bridge: listening on tcp 127.0.0.1:13501", 4000);
}

void onGameEnd(const unsigned int, void*, void*)
{
}

void onUpdatePhysics(const unsigned int, void*, void*)
{
   if (!vpxApi)
      return;

   // Apply pending commands (press/release are distinct -> cradling works).
   const bool l = g_flipLeft.load();
   if (l != g_flipLeftApplied)
   {
      vpxApi->SetActionState(VPXACTION_LeftFlipper, l ? 1 : 0);
      g_flipLeftApplied = l;
   }
   const bool r = g_flipRight.load();
   if (r != g_flipRightApplied)
   {
      vpxApi->SetActionState(VPXACTION_RightFlipper, r ? 1 : 0);
      g_flipRightApplied = r;
   }

   // Snapshot telemetry.
   Snapshot snap;
   snap.tick = ++g_tick;
   snap.time = vpxApi->GetGameTime ? vpxApi->GetGameTime() : 0.0;
   snap.nballs = vpxApi->GetBalls ? vpxApi->GetBalls(snap.balls, kMaxBalls) : 0;
   if (snap.nballs > kMaxBalls)
      snap.nballs = kMaxBalls;
   snap.nflippers = vpxApi->GetFlippers ? vpxApi->GetFlippers(snap.flippers, kMaxFlippers) : 0;
   if (snap.nflippers > kMaxFlippers)
      snap.nflippers = kMaxFlippers;

   {
      std::lock_guard<std::mutex> lk(g_txMutex);
      g_snap = snap;
      ++g_seq;
   }
   g_txCv.notify_one();
}

} // namespace BotBridge

using namespace BotBridge;

MSGPI_EXPORT void MSGPIAPI BotBridgePluginLoad(const uint32_t sessionId, const MsgPluginAPI* api)
{
   msgApi = api;
   endpointId = sessionId;

   msgApi->BroadcastMsg(endpointId, getVpxApiId = msgApi->GetMsgID(VPXPI_NAMESPACE, VPXPI_MSG_GET_API), &vpxApi);
   msgApi->SubscribeMsg(endpointId, onGameStartId = msgApi->GetMsgID(VPXPI_NAMESPACE, VPXPI_EVT_ON_GAME_START), onGameStart, nullptr);
   msgApi->SubscribeMsg(endpointId, onGameEndId = msgApi->GetMsgID(VPXPI_NAMESPACE, VPXPI_EVT_ON_GAME_END), onGameEnd, nullptr);
   msgApi->SubscribeMsg(endpointId, onUpdatePhysicsId = msgApi->GetMsgID(VPXPI_NAMESPACE, VPXPI_EVT_ON_UPDATE_PHYSICS), onUpdatePhysics, nullptr);

#ifdef _WIN32
   WSADATA wsa;
   WSAStartup(MAKEWORD(2, 2), &wsa);
#endif
   g_running.store(true);
   g_serverThread = std::thread(serverLoop);
}

MSGPI_EXPORT void MSGPIAPI BotBridgePluginUnload()
{
   // Stop the network threads first (they touch no bus/engine API).
   g_running.store(false);
   g_connected.store(false);
   {
      std::lock_guard<std::mutex> lk(g_txMutex);
      ++g_seq;
   }
   g_txCv.notify_all();
   const bb_socket_t ls = g_listenSock.exchange(BB_INVALID_SOCKET);
   if (ls != BB_INVALID_SOCKET)
   {
      shutdown(ls, BB_SHUTDOWN_BOTH); // unblock accept()
      BB_CLOSESOCKET(ls);
   }
   if (g_serverThread.joinable())
      g_serverThread.join();
#ifdef _WIN32
   WSACleanup();
#endif

   // Unsubscribe / release on the main thread (mandatory cleanup).
   msgApi->UnsubscribeMsg(onGameStartId, onGameStart, nullptr);
   msgApi->UnsubscribeMsg(onGameEndId, onGameEnd, nullptr);
   msgApi->UnsubscribeMsg(onUpdatePhysicsId, onUpdatePhysics, nullptr);
   msgApi->ReleaseMsgID(getVpxApiId);
   msgApi->ReleaseMsgID(onGameStartId);
   msgApi->ReleaseMsgID(onGameEndId);
   msgApi->ReleaseMsgID(onUpdatePhysicsId);

   vpxApi = nullptr;
   msgApi = nullptr;
}
