// license:GPLv3+

#include "core/stdafx.h"
#include "VPXPluginAPIImpl.h"

#include "core/VPApp.h"
#include "parts/flasher.h"
#include "parts/ball.h"
#include "parts/flipper.h"
#include "parts/plunger.h"
#include "parts/light.h"
#include "parts/ramp.h"
#include "parts/surface.h"
#include "parts/kicker.h"
#include "parts/gate.h"
#include "parts/spinner.h"
#include "parts/trigger.h"
#include "parts/hittarget.h"
#include "parts/bumper.h"
#include "parts/primitive.h"
#include "parts/dragpoint.h"
#include "physics/PhysicsEngine.h"
#include "renderer/Renderer.h"
#include "ui/live/LiveUI.h"
#include "core/BotBridgeEvents.h"

// Discrete hit/switch event ring for the bot-bridge stream. [bot-bridge]
// Single-threaded in practice: the producer (physics collision/mover code) and the consumer
// (the plugin's GetHitEvents call from its OnUpdatePhysics subscriber) both run on the
// physics/logic thread, so plain indices are safe. Names/types are resolved at drain time.
namespace
{
   struct BBHitRec { double t; IEditable* part; uint32_t kind; float scalar; };
   constexpr unsigned int kBBHitRingSize = 1024; // power of two
   BBHitRec g_bbHitRing[kBBHitRingSize];
   unsigned int g_bbHitHead = 0, g_bbHitTail = 0, g_bbHitDropped = 0;
}

void BotBridge::PushHitEvent(IEditable* part, unsigned int kind, float scalar)
{
   if (!part || !g_pplayer)
      return;
   const unsigned int next = (g_bbHitHead + 1) & (kBBHitRingSize - 1);
   if (next == g_bbHitTail) // full -> drop newest
   {
      ++g_bbHitDropped;
      return;
   }
   BBHitRec& r = g_bbHitRing[g_bbHitHead];
   r.t = g_pplayer->m_time_sec;
   r.part = part;
   r.kind = kind;
   r.scalar = scalar;
   g_bbHitHead = next;
}

///////////////////////////////////////////////////////////////////////////////
// General information API

void MSGPIAPI VPXPluginAPIImpl::GetVpxInfo(VPXInfo* info)
{
   // statics as they need to survive as C string after this function returns
   static string path;
   path = (g_app->m_fileLocator.GetAppPath(FileLocator::AppSubFolder::Root) / ""sv).string();
   static string prefPath;
   prefPath = (g_app->m_fileLocator.GetAppPath(FileLocator::AppSubFolder::Preferences) / ""sv).string();
   info->path = path.c_str();
   info->prefPath = prefPath.c_str();
}

void MSGPIAPI VPXPluginAPIImpl::GetTableInfo(VPXTableInfo* info)
{
   // Only valid in game
   if (g_pplayer != nullptr)
   {
      // static as it needs to survive as C string after this function returns
      static string filepath;
      filepath = g_pplayer->m_ptable->m_filename.string();
      info->path = filepath.c_str();
      info->tableWidth = g_pplayer->m_ptable->m_right;
      info->tableHeight = g_pplayer->m_ptable->m_bottom;
   }
   else
   {
      memset(info, 0, sizeof(VPXTableInfo));
   }
}


///////////////////////////////////////////////////////////////////////////////
// User Input API

unsigned int MSGPIAPI VPXPluginAPIImpl::PushNotification(const char* msg, const int lengthMs)
{
   assert(g_pplayer); // Only allowed in game
   return g_pplayer->m_liveUI->PushNotification(msg, lengthMs);
}

void MSGPIAPI VPXPluginAPIImpl::UpdateNotification(const unsigned int handle, const char* msg, const int lengthMs)
{
   assert(g_pplayer); // Only allowed in game
   g_pplayer->m_liveUI->PushNotification(msg, lengthMs, handle);
}


///////////////////////////////////////////////////////////////////////////////
// View API

void MSGPIAPI VPXPluginAPIImpl::DisableStaticPrerendering(const BOOL disable)
{
   assert(g_pplayer); // Only allowed in game
   g_pplayer->m_renderer->DisableStaticPrePass(disable);
}

void MSGPIAPI VPXPluginAPIImpl::GetActiveViewSetup(VPXViewSetupDef* view)
{
   assert(g_pplayer); // Only allowed in game
   const ViewSetup& viewSetup = g_pplayer->m_ptable->GetViewSetup();
   view->viewMode = viewSetup.mMode;
   view->sceneScaleX = viewSetup.mSceneScaleX;
   view->sceneScaleY = viewSetup.mSceneScaleY;
   view->sceneScaleZ = viewSetup.mSceneScaleZ;
   view->viewX = viewSetup.mViewX;
   view->viewY = viewSetup.mViewY;
   view->viewZ = viewSetup.mViewZ;
   view->lookAt = viewSetup.mLookAt;
   view->viewportRotation = viewSetup.mViewportRotation;
   view->FOV = viewSetup.mFOV;
   view->layback = viewSetup.mLayback;
   view->viewHOfs = viewSetup.mViewHOfs;
   view->viewVOfs = viewSetup.mViewVOfs;
   view->windowTopZOfs = viewSetup.mWindowTopZOfs;
   view->windowBottomZOfs = viewSetup.mWindowBottomZOfs;
   view->screenWidth = g_pplayer->m_ptable->m_settings.GetPlayer_ScreenWidth();
   view->screenHeight = g_pplayer->m_ptable->m_settings.GetPlayer_ScreenHeight();
   view->screenInclination = g_pplayer->m_ptable->m_settings.GetPlayer_ScreenInclination();
   view->realToVirtualScale = viewSetup.GetRealToVirtualScale(g_pplayer->m_ptable);
}

void MSGPIAPI VPXPluginAPIImpl::SetActiveViewSetup(VPXViewSetupDef* view)
{
   assert(g_pplayer); // Only allowed in game
   ViewSetup& viewSetup = g_pplayer->m_ptable->GetViewSetup();
   viewSetup.mViewX = view->viewX;
   viewSetup.mViewY = view->viewY;
   viewSetup.mViewZ = view->viewZ;
   g_pplayer->m_renderer->InitLayout();
}


///////////////////////////////////////////////////////////////////////////////
// Input API

void MSGPIAPI VPXPluginAPIImpl::SetActionState(const VPXAction actionId, const int isPressed)
{
   if (!g_pplayer)
      return; // No game in progress

   VPXPluginAPIImpl& me = g_pplayer->m_pluginAPI;
   auto it = me.m_actionMap.find(actionId);
   if (it == me.m_actionMap.end())
      return; // action not mapped

   const std::unique_ptr<InputAction>& action = g_pplayer->m_pininput.GetInputActions()[it->second.first];
   if (it->second.second == -1)
      it->second.second = action->NewDirectStateSlot();
   action->SetDirectState(it->second.second, isPressed != 0);
}

void MSGPIAPI VPXPluginAPIImpl::SetNudgeState(const int stateMask, const float nudgeAccelerationX, const float nudgeAccelerationY)
{
   if (!g_pplayer)
      return; // No game in progress

   g_pplayer->m_pininput.SetNudge((stateMask & 1) != 0, nudgeAccelerationX, nudgeAccelerationY);
}

void MSGPIAPI VPXPluginAPIImpl::SetPlungerState(const int stateMask, const float plungerPos, const float plungerSpeed)
{
   if (!g_pplayer)
      return; // No game in progress

   g_pplayer->m_pininput.SetPlungerPos((stateMask & 1) == 0x01, plungerPos);
   g_pplayer->m_pininput.SetPlungerSpeed((stateMask & 3) == 0x03, plungerSpeed); // With speed and overriden
}


///////////////////////////////////////////////////////////////////////////////
// Game State

double MSGPIAPI VPXPluginAPIImpl::GetGameTime()
{
   return g_pplayer ? g_pplayer->m_time_sec : 0.0;
}

// Live game-state telemetry (read-only). [bot-bridge extension]
// Reads engine internals (the ball list / table parts) that are not otherwise
// reachable across the C-ABI plugin boundary, and exposes them as a small,
// table-agnostic, read-only surface. Only valid in game; intended to be called
// from a VPX callback (e.g. an OnUpdatePhysics subscriber) so the state read is
// consistent with the physics step and stays on the API thread.
unsigned int MSGPIAPI VPXPluginAPIImpl::GetBalls(VPXBallState* out, const unsigned int maxCount)
{
   if (!g_pplayer)
      return 0;

   const vector<Ball*>& balls = g_pplayer->m_vball; // every live ball (multiball-safe)
   const unsigned int total = static_cast<unsigned int>(balls.size());
   for (unsigned int i = 0; i < total && i < maxCount; ++i)
   {
      Ball* const pBall = balls[i];
      VPXBallState& b = out[i];

      int id = 0;
      pBall->get_ID(&id);
      b.id = static_cast<uint32_t>(id);

      const Vertex3Ds& pos = pBall->m_hitBall.m_d.m_pos;
      const Vertex3Ds& vel = pBall->m_hitBall.m_d.m_vel;
      b.x = pos.x; b.y = pos.y; b.z = pos.z;
      b.vx = vel.x; b.vy = vel.y; b.vz = vel.z;

      // Engine stores angular momentum; angular velocity = L / I (solid sphere)
      const float inertia = pBall->m_hitBall.Inertia();
      const Vertex3Ds& angMom = pBall->m_hitBall.m_angularmomentum;
      if (inertia > 0.f)
      {
         b.angVelX = angMom.x / inertia;
         b.angVelY = angMom.y / inertia;
         b.angVelZ = angMom.z / inertia;
      }
      else
         b.angVelX = b.angVelY = b.angVelZ = 0.f;

      b.radius = pBall->m_hitBall.m_d.m_radius;
      b.mass = pBall->m_hitBall.m_d.m_mass;
   }
   return total;
}

unsigned int MSGPIAPI VPXPluginAPIImpl::GetFlippers(VPXFlipperState* out, const unsigned int maxCount)
{
   if (!g_pplayer)
      return 0;

   unsigned int total = 0;
   for (IEditable* const pedit : g_pplayer->m_ptable->GetParts())
   {
      if (pedit->GetItemType() != eItemFlipper)
         continue;
      if (total < maxCount)
      {
         Flipper* const pflip = static_cast<Flipper*>(pedit);
         float angle = 0.f;
         pflip->get_CurrentAngle(&angle); // live angle in degrees
         out[total].angle = angle;
         out[total].angleSpeed = pflip->GetAngleSpeedDeg(); // deg per VP-tick
         out[total].solenoid = pflip->IsSolenoidActive() ? 1 : 0;
         out[total].endOfStroke = pflip->IsAtEndOfStroke() ? 1 : 0;
      }
      ++total;
   }
   return total;
}

unsigned int MSGPIAPI VPXPluginAPIImpl::GetPlungers(VPXPlungerState* out, const unsigned int maxCount)
{
   if (!g_pplayer)
      return 0;

   unsigned int total = 0;
   for (IEditable* const pedit : g_pplayer->m_ptable->GetParts())
   {
      if (pedit->GetItemType() != eItemPlunger)
         continue;
      if (total < maxCount)
      {
         VPXPlungerState& p = out[total];
         p.position = p.posVPU = p.speed = p.restPos = 0.f;
         if (const PlungerMoverObject* const mv = static_cast<Plunger*>(pedit)->GetMover())
         {
            // 0 = fully forward (m_frameEnd), 1 = fully pulled back (m_frameStart)
            const float denom = mv->m_frameEnd - mv->m_frameStart;
            float norm = (denom != 0.f) ? (mv->m_frameEnd - mv->m_pos) / denom : 0.f;
            norm = (norm < 0.f) ? 0.f : (norm > 1.f ? 1.f : norm);
            p.position = norm;
            p.posVPU = mv->m_pos;
            p.speed = mv->m_speed;
            p.restPos = mv->m_restPos;
         }
      }
      ++total;
   }
   return total;
}

// Clamp a Light::m_inPlayState (0..1 modulated, 2.f = blinking) to the {0,1,2} mode enum.
static int LampModeOf(const float inPlayState)
{
   const long m = lroundf(inPlayState);
   return (m < 0) ? 0 : (m > 2 ? 2 : static_cast<int>(m));
}

// Static lamp directory (sent once at game start); also caches the light pointers so the per-tick
// GetLamps() avoids re-scanning all table parts. Rebuild this whenever a game (re)starts.
unsigned int MSGPIAPI VPXPluginAPIImpl::GetLampDescriptors(VPXLampDesc* out, const unsigned int maxCount)
{
   if (!g_pplayer)
      return 0;

   VPXPluginAPIImpl& me = g_pplayer->m_pluginAPI;
   me.m_lampPtrs.clear();
   unsigned int total = 0;
   for (IEditable* const pedit : g_pplayer->m_ptable->GetParts())
   {
      if (pedit->GetItemType() != eItemLight)
         continue;
      Light* const pLight = static_cast<Light*>(pedit);
      if (total < maxCount)
      {
         out[total].index = total;
         const string name = pedit->GetName();
         snprintf(out[total].name, sizeof(out[total].name), "%s", name.c_str());
         out[total].mode = LampModeOf(pLight->m_inPlayState);
      }
      me.m_lampPtrs.push_back(pLight);
      ++total;
   }
   return total;
}

unsigned int MSGPIAPI VPXPluginAPIImpl::GetLamps(VPXLampState* out, const unsigned int maxCount)
{
   if (!g_pplayer)
      return 0;

   VPXPluginAPIImpl& me = g_pplayer->m_pluginAPI;
   const unsigned int total = static_cast<unsigned int>(me.m_lampPtrs.size());
   for (unsigned int i = 0; i < total && i < maxCount; ++i)
   {
      Light* const pLight = me.m_lampPtrs[i];
      out[i].mode = LampModeOf(pLight->m_inPlayState);
      // Derive on/off + analog value from the live faded emission (m_currentIntensity): this
      // captures dim and blink phase, and avoids GetInPlayStateBool()'s blink-pattern string
      // indexing (which can trip an MSVC-debug subscript assert when called every tick).
      const float intensity = pLight->m_currentIntensity;
      out[i].intensity = intensity;
      out[i].lit = (intensity > 0.01f) ? 1 : 0;
   }
   return total;
}

void MSGPIAPI VPXPluginAPIImpl::GetTableState(VPXTableState* out)
{
   memset(out, 0, sizeof(VPXTableState));
   if (!g_pplayer || !g_pplayer->m_physics)
      return;
   PhysicsEngine* const phys = g_pplayer->m_physics;
   const Vertex3Ds nudgeAcc = phys->GetNudgeAcceleration();
   out->nudgeAccelX = nudgeAcc.x;
   out->nudgeAccelY = nudgeAcc.y;
   const Vertex2D vel = phys->GetTableVelocity();
   out->tableVelX = vel.x;
   out->tableVelY = vel.y;
   const Vertex2D disp = phys->GetTableDisplacement();
   out->tableDispX = disp.x;
   out->tableDispY = disp.y;
   // Bounds-check the action ids: IsPressed() asserts on an out-of-range id, and some actions
   // (e.g. slam tilt) may be unmapped on a given table/config.
   InputManager& in = g_pplayer->m_pininput;
   const size_t nActions = in.GetInputActions().size();
   const size_t tiltId = static_cast<size_t>(in.GetTiltActionId());
   const size_t slamId = static_cast<size_t>(in.GetSlamTiltActionId());
   out->tiltActive = (tiltId < nActions && in.IsPressed(static_cast<int>(tiltId))) ? 1 : 0;
   out->slamTiltActive = (slamId < nActions && in.IsPressed(static_cast<int>(slamId))) ? 1 : 0;
   out->plumbSimulated = phys->IsPlumbSimulated() ? 1 : 0;
   out->plumbTiltCount = phys->GetPlumbTiltIndex();
}

// Static geometry of every collidable part, read once at game start. Field meanings are
// type-specific (documented in plugins/bot-bridge/README.md). All lengths VPU, angles degrees.
unsigned int MSGPIAPI VPXPluginAPIImpl::GetGeometry(VPXPartGeom* out, const unsigned int maxCount)
{
   if (!g_pplayer)
      return 0;

   unsigned int total = 0;
   for (IEditable* const pedit : g_pplayer->m_ptable->GetParts())
   {
      const ItemTypeEnum type = pedit->GetItemType();
      bool wanted = true;
      VPXPartGeom g;
      memset(&g, 0, sizeof(g));
      g.type = static_cast<uint32_t>(type);

      switch (type)
      {
      case eItemFlipper:
      {
         const FlipperData& d = static_cast<Flipper*>(pedit)->m_d;
         g.x = d.m_Center.x; g.y = d.m_Center.y; g.z = d.m_height;
         g.a = d.m_FlipperRadiusMax; g.b = d.m_BaseRadius; g.c = d.m_EndRadius; g.d = d.m_StartAngle;
         g.ex = d.m_EndAngle;
         break;
      }
      case eItemRamp:
      {
         Ramp* const r = static_cast<Ramp*>(pedit);
         const RampData& d = r->m_d;
         if (!r->m_vdpoint.empty())
         {
            const Vertex3Ds& entrance = r->m_vdpoint.front()->m_v; // bottom end
            const Vertex3Ds& exit = r->m_vdpoint.back()->m_v;      // top end
            g.x = entrance.x; g.y = entrance.y; g.z = d.m_heightbottom;
            g.ex = exit.x; g.ey = exit.y; g.ez = d.m_heighttop;
         }
         g.a = d.m_widthbottom; g.b = d.m_widthtop; g.c = static_cast<float>(d.m_type);
         break;
      }
      case eItemSurface:
      {
         Surface* const s = static_cast<Surface*>(pedit);
         const SurfaceData& d = s->m_d;
         float minx = 1e30f, miny = 1e30f, maxx = -1e30f, maxy = -1e30f;
         for (const CComObject<DragPoint>* const dp : s->m_vdpoint)
         {
            minx = min(minx, dp->m_v.x); miny = min(miny, dp->m_v.y);
            maxx = max(maxx, dp->m_v.x); maxy = max(maxy, dp->m_v.y);
         }
         if (!s->m_vdpoint.empty()) { g.x = minx; g.y = miny; g.ex = maxx; g.ey = maxy; }
         g.a = d.m_heightbottom; g.b = d.m_heighttop;
         break;
      }
      case eItemKicker:
      {
         const KickerData& d = static_cast<Kicker*>(pedit)->m_d;
         g.x = d.m_vCenter.x; g.y = d.m_vCenter.y; g.z = d.m_hit_height;
         g.a = d.m_radius; g.b = static_cast<float>(d.m_kickertype); g.c = d.m_orientation;
         break;
      }
      case eItemTrigger:
      {
         const TriggerData& d = static_cast<Trigger*>(pedit)->m_d;
         g.x = d.m_vCenter.x; g.y = d.m_vCenter.y; g.z = d.m_hit_height;
         g.a = d.m_radius; g.b = static_cast<float>(d.m_shape); g.c = d.m_rotation;
         break;
      }
      case eItemGate:
      {
         const GateData& d = static_cast<Gate*>(pedit)->m_d;
         g.x = d.m_vCenter.x; g.y = d.m_vCenter.y; g.z = d.m_height;
         g.a = d.m_length; g.b = d.m_rotation; g.c = d.m_angleMin; g.d = d.m_angleMax;
         break;
      }
      case eItemSpinner:
      {
         const SpinnerData& d = static_cast<Spinner*>(pedit)->m_d;
         g.x = d.m_vCenter.x; g.y = d.m_vCenter.y; g.z = d.m_height;
         g.a = d.m_length; g.b = d.m_rotation; g.c = d.m_angleMin; g.d = d.m_angleMax;
         break;
      }
      case eItemHitTarget:
      {
         const HitTargetData& d = static_cast<HitTarget*>(pedit)->m_d;
         g.x = d.m_vPosition.x; g.y = d.m_vPosition.y; g.z = d.m_vPosition.z;
         g.ex = d.m_vSize.x; g.ey = d.m_vSize.y; g.ez = d.m_vSize.z;
         g.a = d.m_rotZ; g.b = static_cast<float>(d.m_targetType); g.c = d.m_isDropped ? 1.f : 0.f;
         break;
      }
      case eItemBumper:
      {
         const BumperData& d = static_cast<Bumper*>(pedit)->m_d;
         g.x = d.m_vCenter.x; g.y = d.m_vCenter.y; g.a = d.m_radius;
         break;
      }
      case eItemPrimitive:
      {
         const PrimitiveData& d = static_cast<Primitive*>(pedit)->m_d;
         g.x = d.m_vPosition.x; g.y = d.m_vPosition.y; g.z = d.m_vPosition.z;
         g.ex = d.m_vSize.x; g.ey = d.m_vSize.y; g.ez = d.m_vSize.z;
         break;
      }
      default:
         wanted = false;
         break;
      }

      if (!wanted)
         continue;
      if (total < maxCount)
      {
         const string name = pedit->GetName();
         snprintf(g.name, sizeof(g.name), "%s", name.c_str());
         out[total] = g;
      }
      ++total;
   }
   return total;
}

// Drain up to maxCount queued hit/switch events; returns the number written. Call repeatedly
// while it returns maxCount to fully drain. Names/types resolved here (off the collision hot path).
unsigned int MSGPIAPI VPXPluginAPIImpl::GetHitEvents(VPXHitEvent* out, const unsigned int maxCount)
{
   if (!g_pplayer)
      return 0;
   unsigned int n = 0;
   while (g_bbHitTail != g_bbHitHead && n < maxCount)
   {
      const BBHitRec& r = g_bbHitRing[g_bbHitTail];
      VPXHitEvent& e = out[n];
      e.timeSec = r.t;
      e.partType = r.part ? static_cast<uint32_t>(r.part->GetItemType()) : 0xFFFFFFFFu;
      e.eventKind = r.kind;
      e.scalar = r.scalar;
      const string nm = r.part ? r.part->GetName() : string();
      snprintf(e.name, sizeof(e.name), "%s", nm.c_str());
      ++n;
      g_bbHitTail = (g_bbHitTail + 1) & (kBBHitRingSize - 1);
   }
   return n;
}


///////////////////////////////////////////////////////////////////////////////
// Rendering

// We define VPXTexture as a pointer to a VPXTextureBlock holding a reference counted BaseTexture pointer
// This is needed since BaseTexture are referenced both wy their creator and by the pending GPU update (to avoid needing an expensive data copy)
struct VPXTextureBlock
{
   std::shared_ptr<BaseTexture> tex;
   VPXTextureInfo info;
};

static void UpdateVPXTextureInfo(VPXTextureBlock* tex)
{
   tex->info.width = tex->tex->width();
   tex->info.height = tex->tex->height();
   switch (tex->tex->m_format)
   {
   case BaseTexture::BW_FP32: tex->info.format = VPXTextureFormat::VPXTEXFMT_BW32F; break;
   case BaseTexture::SRGB: tex->info.format = VPXTextureFormat::VPXTEXFMT_sRGB8; break;
   case BaseTexture::SRGBA: tex->info.format = VPXTextureFormat::VPXTEXFMT_sRGBA8; break;
   case BaseTexture::SRGB565: tex->info.format = VPXTextureFormat::VPXTEXFMT_sRGB565; break;
   default: break; // FIXME what should we do ? reject texture ? have an unknown data format ?
   }
   tex->info.data = tex->tex->data();
}

std::shared_ptr<BaseTexture> VPXPluginAPIImpl::GetTexture(VPXTexture texture) const
{
   VPXTextureBlock* tex = reinterpret_cast<VPXTextureBlock*>(texture);
   return tex->tex;
}

void MSGPIAPI VPXPluginAPIImpl::UpdateTexture(VPXTexture* texture, int width, int height, VPXTextureFormat format, const void* image)
{
   VPXTextureBlock** tex = reinterpret_cast<VPXTextureBlock**>(texture);
   if (*tex == nullptr)
      *tex = new VPXTextureBlock();
   switch (format)
   {
   case VPXTextureFormat::VPXTEXFMT_BW32F: BaseTexture::Update((*tex)->tex, width, height, BaseTexture::BW_FP32, image); break;
   case VPXTextureFormat::VPXTEXFMT_sRGB8: BaseTexture::Update((*tex)->tex, width, height, BaseTexture::SRGB, image); break;
   case VPXTextureFormat::VPXTEXFMT_sRGBA8: BaseTexture::Update((*tex)->tex, width, height, BaseTexture::SRGBA, image); break;
   case VPXTextureFormat::VPXTEXFMT_sRGB565: BaseTexture::Update((*tex)->tex, width, height, BaseTexture::SRGB565, image); break;
   default: assert(false);
   }
   UpdateVPXTextureInfo(*tex);
}

VPXTexture MSGPIAPI VPXPluginAPIImpl::CreateTexture(uint8_t* rawData, int size)
{
   // BGFX allows to create texture from any thread and other rendering backends are single threaded
   // assert(std::this_thread::get_id() == g_pplayer->m_pluginAPI.m_apiThread);
   VPXTextureBlock* tex = new VPXTextureBlock();
   tex->tex = BaseTexture::CreateFromData(rawData, size);
   if (tex->tex == nullptr)
      return nullptr;
   UpdateVPXTextureInfo(tex);
   return reinterpret_cast<VPXTexture>(tex);
}

VPXTextureInfo* MSGPIAPI VPXPluginAPIImpl::GetTextureInfo(VPXTexture texture)
{
   //assert(std::this_thread::get_id() == g_pplayer->m_pluginAPI.m_apiThread);
   VPXTextureBlock* tex = reinterpret_cast<VPXTextureBlock*>(texture);
   return tex ? &tex->info : nullptr;
}

void MSGPIAPI VPXPluginAPIImpl::DeleteTexture(VPXTexture texture)
{
   g_pplayer->m_pluginManager.GetMsgAPI().RunOnMainThread(
      g_pplayer->m_pluginAPI.GetVPXEndPointId(), 0, 
      [](void* context) {
      VPXTextureBlock* tex = reinterpret_cast<VPXTextureBlock*>(context);
      if (tex)
      {
         if (tex->tex && g_pplayer && g_pplayer->GetCloseState() != Player::CS_CLOSED)
            g_pplayer->m_renderer->m_renderDevice->m_texMan.UnloadTexture(tex->tex.get());
         tex->tex = nullptr;
         delete tex;
      }
   }, texture);
}


///////////////////////////////////////////////////////////////////////////////
// Shared logging support for plugin API

void MSGPIAPI VPXPluginAPIImpl::PluginLog(const char* source, const char* func, int line, unsigned int level, const char* message)
{
   plog::Severity severity;
   switch (level)
   {
   case LPI_LVL_DEBUG: severity = plog::debug; break;
   case LPI_LVL_INFO: severity = plog::info; break;
   case LPI_LVL_WARN: severity = plog::warning; break;
   case LPI_LVL_ERROR: severity = plog::error; break;
   default: assert(false); severity = plog::error; message = "Invalid plugin log message level"; break;
   }
   auto* const logger = plog::get<PLOG_DEFAULT_INSTANCE_ID>();
   if (logger == nullptr || !logger->checkSeverity(severity))
      return;
   const std::string cleanFunc = plog::util::processFuncName(func);
   const std::string where = (source != nullptr && *source != '\0') ? (std::string(source) + ':' + cleanFunc) : cleanFunc;
   *logger += plog::Record(severity, where.c_str(), static_cast<size_t>(line), "", nullptr, PLOG_DEFAULT_INSTANCE_ID).ref() << message;
}


///////////////////////////////////////////////////////////////////////////////
// Script support for plugin API

void MSGPIAPI VPXPluginAPIImpl::RegisterScriptClass(ScriptClassDef* classDef)
{
   VPXPluginAPIImpl& pi = g_pplayer->m_pluginAPI;
   pi.m_dynamicTypeLibrary.RegisterScriptClass(classDef);
}

void MSGPIAPI VPXPluginAPIImpl::RegisterScriptTypeAlias(const char* name, const char* aliasedType)
{
   VPXPluginAPIImpl& pi = g_pplayer->m_pluginAPI;
   pi.m_dynamicTypeLibrary.RegisterScriptTypeAlias(name, aliasedType);
}

void MSGPIAPI VPXPluginAPIImpl::RegisterScriptArray(ScriptArrayDef* arrayDef)
{
   VPXPluginAPIImpl& pi = g_pplayer->m_pluginAPI;
   pi.m_dynamicTypeLibrary.RegisterScriptArray(arrayDef);
}

void MSGPIAPI VPXPluginAPIImpl::SubmitTypeLibrary(const unsigned int endpointId)
{
   VPXPluginAPIImpl& pi = g_pplayer->m_pluginAPI;
   pi.m_scriptContributors.push_back(endpointId);
   pi.m_dynamicTypeLibrary.ResolveAllClasses();
}

bool VPXPluginAPIImpl::IsScriptContributor(const unsigned int endpointId) const { return std::ranges::find(m_scriptContributors, endpointId) != m_scriptContributors.end(); }

void MSGPIAPI VPXPluginAPIImpl::OnScriptError(unsigned int type, const char* message)
{
   VPXPluginAPIImpl& pi = g_pplayer->m_pluginAPI;
   // FIXME implement in DynamicDispatch
}

ScriptClassDef* MSGPIAPI VPXPluginAPIImpl::GetClassDef(const char* typeName)
{
   VPXPluginAPIImpl& pi = g_pplayer->m_pluginAPI;
   return pi.m_dynamicTypeLibrary.ResolveClass(typeName);
}

void MSGPIAPI VPXPluginAPIImpl::UnregisterScriptClass(ScriptClassDef* classDef)
{
   VPXPluginAPIImpl& pi = g_pplayer->m_pluginAPI;
   pi.m_dynamicTypeLibrary.UnregisterScriptClass(classDef);
}

void MSGPIAPI VPXPluginAPIImpl::UnregisterScriptTypeAlias(const char* name)
{
   VPXPluginAPIImpl& pi = g_pplayer->m_pluginAPI;
   pi.m_dynamicTypeLibrary.UnregisterScriptTypeAlias(name);
}

void MSGPIAPI VPXPluginAPIImpl::UnregisterScriptArray(ScriptArrayDef* arrayDef)
{
   VPXPluginAPIImpl& pi = g_pplayer->m_pluginAPI;
   pi.m_dynamicTypeLibrary.UnregisterScriptArray(arrayDef);
}

///////////////////////////////////////////////////////////////////////////////
// API to support overriding legacy COM objects

void MSGPIAPI VPXPluginAPIImpl::SetCOMObjectOverride(const char* className, const ScriptClassDef* classDef)
{
   VPXPluginAPIImpl& pi = g_pplayer->m_pluginAPI;
   // FIXME remove when classDef is unregistered
   // FIXME check that classDef has been registered in the type library ?
   const string classId(lowerCase(className));
   if (classDef == nullptr)
      pi.m_scriptCOMObjectOverrides.erase(classId);
   else
      pi.m_scriptCOMObjectOverrides[classId] = classDef;
}

#include <regex>
string VPXPluginAPIImpl::ApplyScriptCOMObjectOverrides(const string& script) const
{
   if (m_scriptCOMObjectOverrides.empty())
      return script;
   std::regex re(R"(CreateObject\(\s*\"(.*)\"\s*\))", std::regex::icase);
   std::smatch res;
   string::const_iterator searchStart(script.cbegin());
   std::stringstream result;
   while (std::regex_search(searchStart, script.cend(), res, re))
   {
      result << res.prefix().str();
      const string className = lowerCase(res[1].str());
      const auto& overrideEntry = m_scriptCOMObjectOverrides.find(className);
      if (overrideEntry != m_scriptCOMObjectOverrides.end())
      {
         PLOGI << "COM script object " << className << " overriden to be provided by a plugin";
         result << "CreatePluginObject(\"" << className << "\")";
      }
      else
      {
         result << res.str();
      }
      searchStart = res.suffix().first;
   }
   result << std::string(searchStart, script.cend());
   return result.str();
}

IDispatch* VPXPluginAPIImpl::CreateCOMPluginObject(const string& classId)
{
   // FIXME we are not separating type library per plugin, therefore collision may occur
   VPXPluginAPIImpl& pi = g_pplayer->m_pluginAPI;
   const string className(lowerCase(classId));
   const auto& overrideEntry = m_scriptCOMObjectOverrides.find(className);
   if (overrideEntry == m_scriptCOMObjectOverrides.end())
   {
      PLOGE << "Asked to create object of type " << classId << " which is not registered";
      return nullptr;

   }
   const ScriptClassDef* classDef = overrideEntry->second;
   if (classDef->CreateObject == nullptr)
   {
      PLOGE << "Asked to create object of type " << classId << " which is registered without a factory method";
      return nullptr;
   }
   void* pScriptObject = classDef->CreateObject();
   if (pScriptObject == nullptr)
   {
      PLOGE << "Failed to create object of class " << classId;
      return nullptr;
   }
   DynamicDispatch* dd = new DynamicDispatch(&pi.m_dynamicTypeLibrary, classDef, pScriptObject);
   PSC_RELEASE(classDef, pScriptObject);
   return dd;
}

///////////////////////////////////////////////////////////////////////////////
// Settings API

void VPXPluginAPIImpl::UpdateSetting(const std::string& pluginId, MsgPI::MsgPluginManager::SettingAction action, MsgSettingDef* settingDef)
{
   if (action == MsgPI::MsgPluginManager::SettingAction::UnregisterAll)
   {
      // We keep the property definition in the settings, but we remove reference to the plugin owned memory block with live data
      m_pluginSettings.erase(
         std::remove_if(m_pluginSettings.begin(), m_pluginSettings.end(), [&pluginId](const PluginSetting& x) { return x.pluginId == pluginId; }), m_pluginSettings.end());
      return;
   }

   const auto item = std::ranges::find_if(
      m_pluginSettings, [&pluginId, &settingDef](const PluginSetting& setting) { return setting.pluginId == pluginId && setting.setting->propId == settingDef->propId; });

   // Register property and get or set value
   Settings& settings = g_pplayer ? g_pplayer->m_ptable->m_settings : g_app->m_settings;
   const bool asTableOverride = g_pplayer != nullptr;
   const std::string sectionName = "Plugin."s + pluginId;
   switch (settingDef->type)
   {
   case MSGPI_SETTING_TYPE_FLOAT:
   {
      const auto newId = Settings::GetRegistry().Register(std::make_unique<VPX::Properties::FloatPropertyDef>(sectionName, settingDef->propId, settingDef->name, settingDef->description,
         false,
         settingDef->floatDef.minVal, settingDef->floatDef.maxVal, settingDef->floatDef.step, settingDef->floatDef.defVal));
      if (item == m_pluginSettings.end())
         m_pluginSettings.emplace_back(pluginId, newId, settingDef);
      else
      {
         item->propId = newId;
         item->setting = settingDef;
      }
      if (action == MsgPI::MsgPluginManager::SettingAction::Save)
         settings.Set(newId, settingDef->floatDef.Get(), asTableOverride);
      else if (action == MsgPI::MsgPluginManager::SettingAction::Load)
         settingDef->floatDef.Set(settings.GetFloat(newId));
      break;
   }

   case MSGPI_SETTING_TYPE_INT:
      if (settingDef->intDef.values)
      {
         vector<string> values;
         for (int i = settingDef->intDef.minVal; i <= settingDef->intDef.maxVal; i++)
            values.emplace_back(settingDef->intDef.values[i - settingDef->intDef.minVal]);
         const auto newId = Settings::GetRegistry().Register(std::make_unique<VPX::Properties::EnumPropertyDef>(
            sectionName, settingDef->propId, settingDef->name, settingDef->description, false, settingDef->intDef.minVal, settingDef->intDef.defVal, values));
         if (item == m_pluginSettings.end())
            m_pluginSettings.emplace_back(pluginId, newId, settingDef);
         else
         {
            item->propId = newId;
            item->setting = settingDef;
         }
         if (action == MsgPI::MsgPluginManager::SettingAction::Save)
            settings.Set(newId, settingDef->intDef.Get(), asTableOverride);
         else if (action == MsgPI::MsgPluginManager::SettingAction::Load)
            settingDef->intDef.Set(settings.GetInt(newId));
      }
      else
      {
         const auto newId = Settings::GetRegistry().Register(std::make_unique<VPX::Properties::IntPropertyDef>(
            sectionName, settingDef->propId, settingDef->name, settingDef->description, false, settingDef->intDef.minVal, settingDef->intDef.maxVal, settingDef->intDef.defVal));
         if (item == m_pluginSettings.end())
            m_pluginSettings.emplace_back(pluginId, newId, settingDef);
         else
         {
            item->propId = newId;
            item->setting = settingDef;
         }
         if (action == MsgPI::MsgPluginManager::SettingAction::Save)
            settings.Set(newId, settingDef->intDef.Get(), asTableOverride);
         else if (action == MsgPI::MsgPluginManager::SettingAction::Load)
            settingDef->intDef.Set(settings.GetInt(newId));
      }
      break;

   case MSGPI_SETTING_TYPE_BOOL:
   {
      const auto newId = Settings::GetRegistry().Register(
         std::make_unique<VPX::Properties::BoolPropertyDef>(sectionName, settingDef->propId, settingDef->name, settingDef->description, false, settingDef->boolDef.defVal));
      if (item == m_pluginSettings.end())
         m_pluginSettings.emplace_back(pluginId, newId, settingDef);
      else
      {
         item->propId = newId;
         item->setting = settingDef;
      }
      if (action == MsgPI::MsgPluginManager::SettingAction::Save)
         settings.Set(newId, settingDef->boolDef.Get(), asTableOverride);
      else if (action == MsgPI::MsgPluginManager::SettingAction::Load)
         settingDef->boolDef.Set(settings.GetBool(newId));
      break;
   }

   case MSGPI_SETTING_TYPE_STRING:
   {
      const auto newId = Settings::GetRegistry().Register(
         std::make_unique<VPX::Properties::StringPropertyDef>(sectionName, settingDef->propId, settingDef->name, settingDef->description, false, settingDef->stringDef.defVal));
      if (item == m_pluginSettings.end())
         m_pluginSettings.emplace_back(pluginId, newId, settingDef);
      else
      {
         item->propId = newId;
         item->setting = settingDef;
      }
      if (action == MsgPI::MsgPluginManager::SettingAction::Save)
         settings.Set(newId, settingDef->stringDef.Get(), asTableOverride);
      else if (action == MsgPI::MsgPluginManager::SettingAction::Load)
      {
         const string& value = settings.GetString(newId);
         settingDef->stringDef.Set(value.c_str());
      }
      break;
   }

   }
}


///////////////////////////////////////////////////////////////////////////////
// Expose VPX contributions through plugin API

#include "plugins/ControllerPlugin.h"

void VPXPluginAPIImpl::OnGameStart()
{
   assert(m_dmdSources.empty());
   g_bbHitHead = g_bbHitTail = g_bbHitDropped = 0; // [bot-bridge] reset hit-event ring for the new game
   const auto& msgApi = m_msgApi;

   msgApi.SubscribeMsg(GetVPXEndPointId(), m_onDisplayGetSrcMsgId, &ControllerOnGetDMDSrc, this);

   msgApi.BroadcastMsg(GetVPXEndPointId(), m_onGameStartMsgId, nullptr);

   msgApi.BroadcastMsg(GetVPXEndPointId(), m_onDisplaySrcChgMsgId, nullptr);

   const InputManager& inputManager = g_pplayer->m_pininput;
   m_actionMap[VPXACTION_LeftFlipper] = { inputManager.GetLeftFlipperActionId(), -1 };
   m_actionMap[VPXACTION_RightFlipper] = { inputManager.GetRightFlipperActionId(), -1 };
   m_actionMap[VPXACTION_StagedLeftFlipper] = { inputManager.GetStagedLeftFlipperActionId(), -1 };
   m_actionMap[VPXACTION_StagedRightFlipper] = { inputManager.GetStagedRightFlipperActionId(), -1 };
   m_actionMap[VPXACTION_LeftMagnaSave] = { inputManager.GetLeftMagnaActionId(), -1 };
   m_actionMap[VPXACTION_RightMagnaSave] = { inputManager.GetRightMagnaActionId(), -1 };
   m_actionMap[VPXACTION_LaunchBall] = { inputManager.GetLaunchBallActionId(), -1 };
   m_actionMap[VPXACTION_LeftNudge] = { inputManager.GetLeftNudgeActionId(), -1 };
   m_actionMap[VPXACTION_CenterNudge] = { inputManager.GetCenterNudgeActionId(), -1 };
   m_actionMap[VPXACTION_RightNudge] = { inputManager.GetRightNudgeActionId(), -1 };
   m_actionMap[VPXACTION_Tilt] = { inputManager.GetTiltActionId(), -1 };
   m_actionMap[VPXACTION_AddCredit] = { inputManager.GetAddCreditActionId(0), -1 };
   m_actionMap[VPXACTION_AddCredit2] = { inputManager.GetAddCreditActionId(1), -1 };
   m_actionMap[VPXACTION_StartGame] = { inputManager.GetStartActionId(), -1 };
   m_actionMap[VPXACTION_Lockbar] = { inputManager.GetLockbarActionId(), -1 };
   //m_actionMap[VPXACTION_Pause] = { inputManager.GetPauseActionId(), -1 };
   m_actionMap[VPXACTION_PerfOverlay] = { inputManager.GetLeftFlipperActionId(), -1 };
   m_actionMap[VPXACTION_OpenInGameUI] = { inputManager.GetOpenInGameUIActionId(), -1 };
   m_actionMap[VPXACTION_ExitGame] = { inputManager.GetExitGameActionId(), -1 };
   //m_actionMap[VPXACTION_InGameUI] = { inputManager.GetIn(), -1 };
   m_actionMap[VPXACTION_VolumeDown] = { inputManager.GetVolumeDownActionId(), -1 };
   m_actionMap[VPXACTION_VolumeUp] = { inputManager.GetVolumeUpActionId(), -1 };
   //m_actionMap[VPXACTION_VRRecenter] = { inputManager.GetVRRecenterActionId(), -1 };
   //m_actionMap[VPXACTION_VRUp] = { inputManager.GetVRUpActionId(), -1 };
   //m_actionMap[VPXACTION_VRDown] = { inputManager.GetVRDownActionId(), -1 };
}

void VPXPluginAPIImpl::OnGameEnd()
{
   const auto& msgApi = m_msgApi;

   msgApi.UnsubscribeMsg(m_onDisplayGetSrcMsgId, &ControllerOnGetDMDSrc, this);

   m_dmdSources.clear();

   msgApi.BroadcastMsg(GetVPXEndPointId(), m_onDisplaySrcChgMsgId, nullptr);

   msgApi.BroadcastMsg(GetVPXEndPointId(), m_onGameEndMsgId, nullptr);

   m_actionMap.clear();
}

void VPXPluginAPIImpl::UpdateDMDSource(Flasher* flasher, bool isAdd)
{
   if (flasher)
   {
      if (isAdd)
      {
         if (std::ranges::find(m_dmdSources, flasher) != m_dmdSources.end())
            return;
         m_dmdSources.push_back(flasher);
      }
      else
      {
         if (std::ranges::find(m_dmdSources, flasher) == m_dmdSources.end())
            return;
         RemoveFromVectorSingle(m_dmdSources, flasher);
      }
   }

   const auto& msgApi = m_msgApi;
   msgApi.BroadcastMsg(GetVPXEndPointId(), m_onDisplaySrcChgMsgId, nullptr);
}

DisplayFrame VPXPluginAPIImpl::ControllerOnGetRenderDMD(const CtlResId id)
{
   VPXPluginAPIImpl& me = g_pplayer->m_pluginAPI;

   if ((g_pplayer == nullptr) || (id.endpointId != me.m_vpxPlugin->m_endpointId))
      return { 0, nullptr };

   DisplayFrame result = { 0, nullptr };
   std::shared_ptr<BaseTexture> dmdFrame;
   if (id.resId == 0)
   {
      result.frameId = g_pplayer->m_dmdFrameId;
      dmdFrame = g_pplayer->m_dmdFrame;
   }
   else if (id.resId <= me.m_dmdSources.size())
   {
      const auto& dmdSrc = me.m_dmdSources[id.resId - 1];
      result.frameId = dmdSrc->m_dmdFrameId;
      dmdFrame = dmdSrc->m_dmdFrame;
   }
   if (dmdFrame == nullptr)
      return { 0, nullptr };

   switch (dmdFrame->m_format)
   {
   case BaseTexture::BW_FP32: result.frame = dmdFrame->data(); break;
   case BaseTexture::SRGB: result.frame = dmdFrame->data(); break;
   case BaseTexture::SRGBA: result.frame = dmdFrame->GetAlias(BaseTexture::SRGB)->data(); break;
   default: assert(false); return { 0, nullptr };  // Not yet supported
   }

   return result;
}

void VPXPluginAPIImpl::ControllerOnGetDMDSrc(const unsigned int msgId, void* userData, void* msgData)
{
   GetDisplaySrcMsg& msg = *static_cast<GetDisplaySrcMsg*>(msgData);
   VPXPluginAPIImpl& me = *static_cast<VPXPluginAPIImpl*>(userData);

   // Main DMD defined from script
   if (g_pplayer && g_pplayer->m_dmdFrame)
   {
      if (msg.count < msg.maxEntryCount)
      {
         msg.entries[msg.count] = {};
         msg.entries[msg.count].id = { { me.m_vpxPlugin->m_endpointId, 0 } };
         msg.entries[msg.count].width = g_pplayer->m_dmdFrame->width();
         msg.entries[msg.count].height = g_pplayer->m_dmdFrame->height();
         msg.entries[msg.count].frameFormat = g_pplayer->m_dmdFrame->m_format == BaseTexture::BW_FP32 ? CTLPI_DISPLAY_FORMAT_LUM32F : CTLPI_DISPLAY_FORMAT_SRGB888;
         msg.entries[msg.count].GetRenderFrame = ControllerOnGetRenderDMD;
      }
      msg.count++;
   }

   // Ancillary DMDs defined on flasher objects from script
   for (size_t i = 0; i < me.m_dmdSources.size(); i++)
   {
      const auto& dmdSrc = me.m_dmdSources[i];
      assert(dmdSrc->m_dmdFrame);
      assert(dmdSrc->m_dmdFrame->m_format == BaseTexture::BW_FP32 || dmdSrc->m_dmdFrame->m_format == BaseTexture::SRGB);
      if (msg.count < msg.maxEntryCount)
      {
         msg.entries[msg.count] = {};
         msg.entries[msg.count].id = { { me.m_vpxPlugin->m_endpointId, static_cast<uint32_t>(i + 1) } };
         msg.entries[msg.count].width = dmdSrc->m_dmdFrame->width();
         msg.entries[msg.count].height = dmdSrc->m_dmdFrame->height();
         msg.entries[msg.count].frameFormat = dmdSrc->m_dmdFrame->m_format == BaseTexture::BW_FP32 ? CTLPI_DISPLAY_FORMAT_LUM32F : CTLPI_DISPLAY_FORMAT_SRGB888;
         msg.entries[msg.count].GetRenderFrame = ControllerOnGetRenderDMD;
      }
      msg.count++;
   }
}


///////////////////////////////////////////////////////////////////////////////
// 

VPXPluginAPIImpl::VPXPluginAPIImpl(MsgPI::MsgPluginManager& pluginManager)
   : m_msgApi(pluginManager.GetMsgAPI()) 
   , m_apiThread(std::this_thread::get_id())
   , m_getVPXAPIMsgId(m_msgApi.GetMsgID(VPXPI_NAMESPACE, VPXPI_MSG_GET_API))
   , m_onGameStartMsgId(m_msgApi.GetMsgID(VPXPI_NAMESPACE, VPXPI_EVT_ON_GAME_START))
   , m_onGameEndMsgId(m_msgApi.GetMsgID(VPXPI_NAMESPACE, VPXPI_EVT_ON_GAME_END))
   , m_getLoggingAPIMsgId(m_msgApi.GetMsgID(LOGPI_NAMESPACE, LOGPI_MSG_GET_API))
   , m_getScriptingAPIMsgId(m_msgApi.GetMsgID(SCRIPTPI_NAMESPACE, SCRIPTPI_MSG_GET_API))
   , m_onDisplaySrcChgMsgId(m_msgApi.GetMsgID(CTLPI_NAMESPACE, CTLPI_DISPLAY_ON_SRC_CHG_MSG))
   , m_onDisplayGetSrcMsgId(m_msgApi.GetMsgID(CTLPI_NAMESPACE, CTLPI_DISPLAY_GET_SRC_MSG))
{
   // Message host
   pluginManager.SetSettingsHandler(
      [this](const std::string& pluginId, MsgPI::MsgPluginManager::SettingAction action, MsgSettingDef* settingDef) { UpdateSetting(pluginId, action, settingDef); });

   // VPX API
   m_api.GetVpxInfo = GetVpxInfo;
   m_api.GetTableInfo = GetTableInfo;

   m_api.PushNotification = PushNotification;
   m_api.UpdateNotification = UpdateNotification;

   m_api.DisableStaticPrerendering = DisableStaticPrerendering;
   m_api.GetActiveViewSetup = GetActiveViewSetup;
   m_api.SetActiveViewSetup = SetActiveViewSetup;

   m_api.SetActionState = SetActionState;
   m_api.SetNudgeState = SetNudgeState;
   m_api.SetPlungerState = SetPlungerState;

   m_api.GetGameTime = GetGameTime;

   m_api.GetBalls = GetBalls;
   m_api.GetFlippers = GetFlippers;
   m_api.GetPlungers = GetPlungers;
   m_api.GetLamps = GetLamps;
   m_api.GetLampDescriptors = GetLampDescriptors;
   m_api.GetGeometry = GetGeometry;
   m_api.GetTableState = GetTableState;
   m_api.GetHitEvents = GetHitEvents;

   m_api.CreateTexture = CreateTexture;
   m_api.UpdateTexture = UpdateTexture;
   m_api.GetTextureInfo = GetTextureInfo;
   m_api.DeleteTexture = DeleteTexture;

   m_vpxPlugin = pluginManager.RegisterPlugin(
      "vpx"s, "VPX"s, "Visual Pinball X"s, ""s, ""s, "https://github.com/vpinball/vpinball"s, //
      [](const uint32_t, const MsgPluginAPI*) { /* Load: nothing to do */ }, //
      []() { /* Load: nothing to do */ });
   m_vpxPlugin->Load(&m_msgApi);
   m_msgApi.SubscribeMsg(m_vpxPlugin->m_endpointId, m_getVPXAPIMsgId, &OnGetVPXPluginAPI, nullptr);

   // Logging API
   m_loggingApi.Log = PluginLog;
   m_msgApi.SubscribeMsg(m_vpxPlugin->m_endpointId, m_getLoggingAPIMsgId, &OnGetLoggingPluginAPI, nullptr);

   // Scriptable API
   m_scriptableApi.RegisterScriptClass = RegisterScriptClass;
   m_scriptableApi.RegisterScriptTypeAlias = RegisterScriptTypeAlias;
   m_scriptableApi.RegisterScriptArrayType = RegisterScriptArray;
   m_scriptableApi.SubmitTypeLibrary = SubmitTypeLibrary;
   m_scriptableApi.SetCOMObjectOverride = SetCOMObjectOverride;
   m_scriptableApi.OnError = OnScriptError;
   m_scriptableApi.GetClassDef = GetClassDef;
   m_scriptableApi.UnregisterScriptClass = UnregisterScriptClass;
   m_scriptableApi.UnregisterScriptTypeAlias = UnregisterScriptTypeAlias;
   m_scriptableApi.UnregisterScriptArrayType = UnregisterScriptArray;
   m_msgApi.SubscribeMsg(m_vpxPlugin->m_endpointId, m_getScriptingAPIMsgId, &OnGetScriptablePluginAPI, nullptr);
}

VPXPluginAPIImpl::~VPXPluginAPIImpl()
{
   m_dynamicTypeLibrary.Reset();
   for (const auto& [a, b] : m_scriptCOMObjectOverrides)
   {
      PLOGE << "An invalid plugin did not unregister COM Object override: " << a;
   }
   m_scriptCOMObjectOverrides.clear();
   m_dmdSources.clear();

   m_msgApi.UnsubscribeMsg(m_getVPXAPIMsgId, &OnGetVPXPluginAPI, nullptr);
   m_msgApi.ReleaseMsgID(m_getVPXAPIMsgId);
   m_msgApi.UnsubscribeMsg(m_getLoggingAPIMsgId, &OnGetLoggingPluginAPI, nullptr);
   m_msgApi.ReleaseMsgID(m_getLoggingAPIMsgId);
   m_msgApi.UnsubscribeMsg(m_getScriptingAPIMsgId, &OnGetScriptablePluginAPI, nullptr);
   m_msgApi.ReleaseMsgID(m_getScriptingAPIMsgId);
   m_msgApi.ReleaseMsgID(m_onGameStartMsgId);
   m_msgApi.ReleaseMsgID(m_onGameEndMsgId);
   m_msgApi.ReleaseMsgID(m_onDisplayGetSrcMsgId);
   m_msgApi.ReleaseMsgID(m_onDisplaySrcChgMsgId);

   // FIXME unregister from MsgAPI plugins (not yet implemented in MsgAPI)
}

void VPXPluginAPIImpl::BroadcastVPXMsg(const unsigned int msgId, void* data) const
{
   m_msgApi.BroadcastMsg(m_vpxPlugin->m_endpointId, msgId, data);
}

unsigned int VPXPluginAPIImpl::GetMsgID(const char* name_space, const char* name) { return m_msgApi.GetMsgID(name_space, name); }

void VPXPluginAPIImpl::ReleaseMsgID(const unsigned int msgId) { m_msgApi.ReleaseMsgID(msgId); }

void VPXPluginAPIImpl::OnGetVPXPluginAPI(const unsigned int msgId, void* userData, void* msgData)
{
   VPXPluginAPIImpl& pi = g_pplayer->m_pluginAPI;
   VPXPluginAPI** pResult = static_cast<VPXPluginAPI**>(msgData);
   *pResult = &pi.m_api;
}

void VPXPluginAPIImpl::OnGetScriptablePluginAPI(const unsigned int msgId, void* userData, void* msgData)
{
   VPXPluginAPIImpl& pi = g_pplayer->m_pluginAPI;
   ScriptablePluginAPI** pResult = static_cast<ScriptablePluginAPI**>(msgData);
   *pResult = &pi.m_scriptableApi;
}

void VPXPluginAPIImpl::OnGetLoggingPluginAPI(const unsigned int msgId, void* userData, void* msgData)
{
   VPXPluginAPIImpl& pi = g_pplayer->m_pluginAPI;
   LoggingPluginAPI** pResult = static_cast<LoggingPluginAPI**>(msgData);
   *pResult = &pi.m_loggingApi;
}
