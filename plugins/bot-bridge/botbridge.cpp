// license:GPLv3+
//
// VPX Bot Bridge plugin (Milestone 2)
// ------------------------------------
// Bridges a running VPX table to an external bot process over loopback TCP (NDJSON).
//
// Two telemetry frame types, distinguished by the "type" field:
//   - "static" : sent once on game start (and to every newly-connected client) — the table's
//                static geometry (collidable parts) and the lamp directory (index -> name).
//   - "state"  : sent every physics update — all balls, full flipper state, plunger, lamps
//                (by index), and global nudge/tilt state.
//
// Commands IN (one NDJSON object per line): digital flip/launch/nudge/tilt (press flag) and
// analog nudge / plunger.
//
// Threading: the MsgPlugin bus + VPX API are not thread-safe, so every API call runs on the
// physics/main thread inside the OnUpdatePhysics / OnGameStart callbacks. All blocking socket
// I/O runs on worker threads; the physics callback only snapshots + notifies.
//
// See plugins/bot-bridge/README.md for the full versioned schema and FINDINGS.md for rationale.

#include "plugins/MsgPlugin.h"
#include "plugins/VPXPlugin.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <string>
#include <vector>
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
static const uint16_t kPort = 13501;
static const unsigned int kSchemaVersion = 2;
static const unsigned int kMaxBalls = 32;
static const unsigned int kMaxFlippers = 16;
static const unsigned int kMaxPlungers = 4;
static const unsigned int kMaxLamps = 256;
static const unsigned int kMaxGeom = 2048;
static const unsigned int kMaxEvents = 128;

// --- plugin / message bus state (main thread only) -----------------------
const MsgPluginAPI* msgApi = nullptr;
VPXPluginAPI* vpxApi = nullptr;
uint32_t endpointId = 0;
unsigned int getVpxApiId = 0, onGameStartId = 0, onGameEndId = 0, onUpdatePhysicsId = 0;

// --- per-tick telemetry snapshot (physics thread -> emitter) --------------
struct Snapshot
{
   uint64_t tick = 0;
   double time = 0.0;
   unsigned int nballs = 0, nflippers = 0, nplungers = 0, nlamps = 0, nevents = 0;
   VPXBallState balls[kMaxBalls];
   VPXFlipperState flippers[kMaxFlippers];
   VPXPlungerState plungers[kMaxPlungers];
   VPXLampState lamps[kMaxLamps];
   VPXTableState table;
   VPXHitEvent events[kMaxEvents];
};

std::mutex g_txMutex;
std::condition_variable g_txCv;
Snapshot g_snap;
uint64_t g_seq = 0;
uint64_t g_tick = 0;

// Static descriptor (geometry + lamp directory), serialized once on game start.
std::mutex g_staticMutex;
std::string g_staticFrame;
std::atomic<uint64_t> g_staticSeq{ 0 };

// --- commands (receiver thread -> physics thread) ------------------------
enum { ACT_FLIP_L, ACT_FLIP_R, ACT_LAUNCH, ACT_NUDGE_L, ACT_NUDGE_C, ACT_NUDGE_R, ACT_TILT, ACT_COUNT };
static const VPXAction kActMap[ACT_COUNT] = {
   VPXACTION_LeftFlipper, VPXACTION_RightFlipper, VPXACTION_LaunchBall,
   VPXACTION_LeftNudge, VPXACTION_CenterNudge, VPXACTION_RightNudge, VPXACTION_Tilt
};
static const char* const kActCmd[ACT_COUNT] = {
   "flip_left", "flip_right", "launch", "nudge_left", "nudge_center", "nudge_right", "tilt"
};
std::atomic<bool> g_act[ACT_COUNT];
bool g_actApplied[ACT_COUNT];

std::atomic<bool> g_nudgeOn{ false };
std::atomic<float> g_nudgeX{ 0.f }, g_nudgeY{ 0.f };
std::atomic<bool> g_plungerOn{ false };
std::atomic<float> g_plungerPos{ 0.f };

// --- networking ----------------------------------------------------------
std::atomic<bool> g_running{ false };
std::atomic<bool> g_connected{ false };
std::atomic<bb_socket_t> g_listenSock{ BB_INVALID_SOCKET };
std::thread g_serverThread;

// -------------------------------------------------------------------------
// Serialization helpers
static void appendJsonStr(std::string& out, const char* s)
{
   out += '"';
   for (; *s; ++s)
   {
      const char c = *s;
      if (c == '"' || c == '\\') { out += '\\'; out += c; }
      else if ((unsigned char)c < 0x20) { /* skip control chars */ }
      else out += c;
   }
   out += '"';
}

static std::string serializeState(const Snapshot& s)
{
   std::string out;
   out.reserve(256 + s.nballs * 160 + s.nflippers * 64 + s.nlamps * 28);
   char buf[256];
   snprintf(buf, sizeof(buf), "{\"type\":\"state\",\"v\":%u,\"tick\":%llu,\"time\":%.6f,\"balls\":[",
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
      const VPXFlipperState& f = s.flippers[i];
      snprintf(buf, sizeof(buf), "%s{\"i\":%u,\"angle\":%.4f,\"angleSpeed\":%.4f,\"solenoid\":%d,\"eos\":%d}",
         i ? "," : "", i, f.angle, f.angleSpeed, f.solenoid, f.endOfStroke);
      out += buf;
   }
   out += "],\"plungers\":[";
   for (unsigned int i = 0; i < s.nplungers; ++i)
   {
      const VPXPlungerState& p = s.plungers[i];
      snprintf(buf, sizeof(buf), "%s{\"i\":%u,\"pos\":%.4f,\"posVPU\":%.3f,\"speed\":%.4f,\"rest\":%.4f}",
         i ? "," : "", i, p.position, p.posVPU, p.speed, p.restPos);
      out += buf;
   }
   out += "],\"lamps\":[";
   for (unsigned int i = 0; i < s.nlamps; ++i)
   {
      const VPXLampState& l = s.lamps[i];
      snprintf(buf, sizeof(buf), "%s{\"i\":%u,\"mode\":%d,\"lit\":%d,\"in\":%.3f}",
         i ? "," : "", i, l.mode, l.lit, l.intensity);
      out += buf;
   }
   const VPXTableState& t = s.table;
   snprintf(buf, sizeof(buf),
      "],\"nudge\":{\"ax\":%.5f,\"ay\":%.5f,\"vx\":%.5f,\"vy\":%.5f,\"dx\":%.4f,\"dy\":%.4f,"
      "\"tilt\":%d,\"slam\":%d,\"plumbSim\":%d,\"plumbCount\":%d}",
      t.nudgeAccelX, t.nudgeAccelY, t.tableVelX, t.tableVelY, t.tableDispX, t.tableDispY,
      t.tiltActive, t.slamTiltActive, t.plumbSimulated, t.plumbTiltCount);
   out += buf;
   out += ",\"events\":[";
   for (unsigned int i = 0; i < s.nevents; ++i)
   {
      const VPXHitEvent& e = s.events[i];
      snprintf(buf, sizeof(buf), "%s{\"t\":%.4f,\"type\":%u,\"kind\":%u,\"scalar\":%.4f,\"name\":",
         i ? "," : "", e.timeSec, e.partType, e.eventKind, e.scalar);
      out += buf;
      appendJsonStr(out, e.name);
      out += '}';
   }
   out += "]}\n";
   return out;
}

static std::string serializeStatic(const VPXPartGeom* geom, unsigned int ngeom,
                                    const VPXLampDesc* lamps, unsigned int nlamps)
{
   std::string out;
   out.reserve(64 + ngeom * 160 + nlamps * 80);
   char buf[256];
   snprintf(buf, sizeof(buf), "{\"type\":\"static\",\"v\":%u,\"geometry\":[", kSchemaVersion);
   out += buf;
   for (unsigned int i = 0; i < ngeom; ++i)
   {
      const VPXPartGeom& g = geom[i];
      snprintf(buf, sizeof(buf), "%s{\"type\":%u,\"name\":", i ? "," : "", g.type);
      out += buf;
      appendJsonStr(out, g.name);
      snprintf(buf, sizeof(buf),
         ",\"x\":%.3f,\"y\":%.3f,\"z\":%.3f,\"a\":%.3f,\"b\":%.3f,\"c\":%.3f,\"d\":%.3f,"
         "\"ex\":%.3f,\"ey\":%.3f,\"ez\":%.3f}",
         g.x, g.y, g.z, g.a, g.b, g.c, g.d, g.ex, g.ey, g.ez);
      out += buf;
   }
   out += "],\"lamps\":[";
   for (unsigned int i = 0; i < nlamps; ++i)
   {
      snprintf(buf, sizeof(buf), "%s{\"i\":%u,\"name\":", i ? "," : "", lamps[i].index);
      out += buf;
      appendJsonStr(out, lamps[i].name);
      snprintf(buf, sizeof(buf), ",\"mode\":%d}", lamps[i].mode);
      out += buf;
   }
   out += "]}\n";
   return out;
}

// -------------------------------------------------------------------------
// Command parsing
static bool parsePress(const std::string& line)
{
   size_t p = line.find("press");
   if (p == std::string::npos) return false;
   p = line.find(':', p);
   if (p == std::string::npos) return false;
   ++p;
   while (p < line.size() && (line[p] == ' ' || line[p] == '\t')) ++p;
   return p < line.size() && (line[p] == 't' || line[p] == 'T' || line[p] == '1');
}

static float parseFloat(const std::string& line, const char* key, float def = 0.f)
{
   size_t p = line.find(key);
   if (p == std::string::npos) return def;
   p = line.find(':', p);
   if (p == std::string::npos) return def;
   return (float)strtod(line.c_str() + p + 1, nullptr);
}

static void handleCommand(const std::string& line)
{
   for (int i = 0; i < ACT_COUNT; ++i)
   {
      if (line.find(kActCmd[i]) != std::string::npos)
      {
         g_act[i].store(parsePress(line));
         return;
      }
   }
   if (line.find("nudge_accel") != std::string::npos)
   {
      g_nudgeX.store(parseFloat(line, "\"x\""));
      g_nudgeY.store(parseFloat(line, "\"y\""));
      g_nudgeOn.store(true);
   }
   else if (line.find("plunger_pos") != std::string::npos)
   {
      g_plungerPos.store(parseFloat(line, "value"));
      g_plungerOn.store(true);
   }
}

// -------------------------------------------------------------------------
// Network workers
static void sendAll(bb_socket_t client, const std::string& s)
{
   size_t sent = 0;
   while (sent < s.size() && g_running.load())
   {
      const int n = (int)send(client, s.data() + sent, (int)(s.size() - sent), 0);
      if (n <= 0) { g_connected.store(false); return; }
      sent += (size_t)n;
   }
}

static void recvLoop(bb_socket_t client)
{
   std::string buf;
   char tmp[2048];
   while (g_running.load() && g_connected.load())
   {
      const int n = (int)recv(client, tmp, sizeof(tmp), 0);
      if (n <= 0) { g_connected.store(false); break; }
      buf.append(tmp, n);
      size_t nl;
      while ((nl = buf.find('\n')) != std::string::npos)
      {
         handleCommand(buf.substr(0, nl));
         buf.erase(0, nl + 1);
      }
   }
}

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
   addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
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
         if (!g_running.load()) break;
         continue;
      }
      setsockopt(client, IPPROTO_TCP, TCP_NODELAY, (const char*)&one, sizeof(one));
      g_connected.store(true);
      std::thread rt(recvLoop, client);

      uint64_t lastState = 0, lastStatic = 0; // force-send static on new connect
      while (g_running.load() && g_connected.load())
      {
         // Send the static descriptor when it changes (new game) or on first iteration of a connection.
         const uint64_t sseq = g_staticSeq.load();
         if (sseq != lastStatic)
         {
            std::string sf;
            { std::lock_guard<std::mutex> lk(g_staticMutex); sf = g_staticFrame; }
            if (!sf.empty()) sendAll(client, sf);
            lastStatic = sseq;
         }

         Snapshot snap;
         {
            std::unique_lock<std::mutex> lk(g_txMutex);
            g_txCv.wait_for(lk, std::chrono::milliseconds(100),
               [&] { return g_seq != lastState || !g_running.load() || !g_connected.load(); });
            if (g_seq == lastState) continue;
            lastState = g_seq;
            snap = g_snap;
         }
         sendAll(client, serializeState(snap));
      }

      g_connected.store(false);
      shutdown(client, BB_SHUTDOWN_BOTH);
      if (rt.joinable()) rt.join();
      BB_CLOSESOCKET(client);
   }

   g_listenSock.store(BB_INVALID_SOCKET);
   BB_CLOSESOCKET(listenSock);
}

// -------------------------------------------------------------------------
// VPX event callbacks (physics / main thread)
void onGameStart(const unsigned int, void*, void*)
{
   for (int i = 0; i < ACT_COUNT; ++i) { g_act[i].store(false); g_actApplied[i] = false; }
   g_nudgeOn.store(false); g_plungerOn.store(false);
   g_tick = 0;
   if (!vpxApi)
      return;

   // Build the one-time static descriptor (geometry + lamp directory). Runs on the main thread,
   // g_pplayer is valid here. GetLampDescriptors also (re)builds the engine-side lamp pointer cache.
   std::vector<VPXPartGeom> geom(kMaxGeom);
   unsigned int ngeom = vpxApi->GetGeometry ? vpxApi->GetGeometry(geom.data(), kMaxGeom) : 0;
   if (ngeom > kMaxGeom) ngeom = kMaxGeom;
   std::vector<VPXLampDesc> lamps(kMaxLamps);
   unsigned int nlamps = vpxApi->GetLampDescriptors ? vpxApi->GetLampDescriptors(lamps.data(), kMaxLamps) : 0;
   if (nlamps > kMaxLamps) nlamps = kMaxLamps;

   std::string sf = serializeStatic(geom.data(), ngeom, lamps.data(), nlamps);
   { std::lock_guard<std::mutex> lk(g_staticMutex); g_staticFrame.swap(sf); }
   g_staticSeq.fetch_add(1);

   vpxApi->PushNotification("Bot Bridge v2: tcp 127.0.0.1:13501", 4000);
}

void onGameEnd(const unsigned int, void*, void*)
{
}

void onUpdatePhysics(const unsigned int, void*, void*)
{
   if (!vpxApi)
      return;

   // Apply digital actions (press/release distinct -> cradling).
   for (int i = 0; i < ACT_COUNT; ++i)
   {
      const bool d = g_act[i].load();
      if (d != g_actApplied[i])
      {
         vpxApi->SetActionState(kActMap[i], d ? 1 : 0);
         g_actApplied[i] = d;
      }
   }
   // Analog overrides (sticky once engaged — see README). Only driven when the bot opted in.
   if (g_nudgeOn.load() && vpxApi->SetNudgeState)
      vpxApi->SetNudgeState(1, g_nudgeX.load(), g_nudgeY.load());
   if (g_plungerOn.load() && vpxApi->SetPlungerState)
      vpxApi->SetPlungerState(1, g_plungerPos.load(), 0.f);

   // Snapshot telemetry.
   Snapshot snap;
   snap.tick = ++g_tick;
   snap.time = vpxApi->GetGameTime ? vpxApi->GetGameTime() : 0.0;
   snap.nballs = vpxApi->GetBalls ? vpxApi->GetBalls(snap.balls, kMaxBalls) : 0;
   if (snap.nballs > kMaxBalls) snap.nballs = kMaxBalls;
   snap.nflippers = vpxApi->GetFlippers ? vpxApi->GetFlippers(snap.flippers, kMaxFlippers) : 0;
   if (snap.nflippers > kMaxFlippers) snap.nflippers = kMaxFlippers;
   snap.nplungers = vpxApi->GetPlungers ? vpxApi->GetPlungers(snap.plungers, kMaxPlungers) : 0;
   if (snap.nplungers > kMaxPlungers) snap.nplungers = kMaxPlungers;
   snap.nlamps = vpxApi->GetLamps ? vpxApi->GetLamps(snap.lamps, kMaxLamps) : 0;
   if (snap.nlamps > kMaxLamps) snap.nlamps = kMaxLamps;
   if (vpxApi->GetTableState) vpxApi->GetTableState(&snap.table);
   else memset(&snap.table, 0, sizeof(snap.table));
   snap.nevents = vpxApi->GetHitEvents ? vpxApi->GetHitEvents(snap.events, kMaxEvents) : 0;
   if (snap.nevents > kMaxEvents) snap.nevents = kMaxEvents;

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
   g_running.store(false);
   g_connected.store(false);
   { std::lock_guard<std::mutex> lk(g_txMutex); ++g_seq; }
   g_txCv.notify_all();
   const bb_socket_t ls = g_listenSock.exchange(BB_INVALID_SOCKET);
   if (ls != BB_INVALID_SOCKET)
   {
      shutdown(ls, BB_SHUTDOWN_BOTH);
      BB_CLOSESOCKET(ls);
   }
   if (g_serverThread.joinable())
      g_serverThread.join();
#ifdef _WIN32
   WSACleanup();
#endif

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
