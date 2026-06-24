// license:GPLv3+

#pragma once

// Lightweight hook for the bot-bridge discrete hit/switch event stream. [bot-bridge]
//
// Physics collision/mover code calls BotBridge::PushHitEvent(...) on the physics thread
// when a ball hits a switch-like object (wall/target/bumper/slingshot/trigger/kicker) or a
// spinner spins. The bot-bridge plugin drains the events through VPXPluginAPI::GetHitEvents
// on each OnUpdatePhysics. Pushing is cheap (a fixed-size ring write, no allocation); the
// part name/type are resolved lazily at drain time. This is the only telemetry hook outside
// src/core; it is additive and keeps the bridge plugin a pure SDK plugin.

class IEditable;

namespace BotBridge
{
   // Keep these in sync with the documented eventKind values in plugins/plugins/VPXPlugin.h.
   enum HitEventKind
   {
      HE_HIT = 0,            // ball hit a collidable (wall/ramp/target/primitive/rubber/bumper/kicker)
      HE_UNHIT = 1,          // ball left a trigger/kicker volume
      HE_SLINGSHOT = 2,      // slingshot fired
      HE_SPIN = 3,           // spinner passed a spin point
      HE_EOS = 4,            // end-of-stroke limit (reserved)
      HE_BOS = 5,            // begin-of-stroke limit (reserved)
      HE_FLIPPER_COLLIDE = 6 // ball-flipper collision (reserved)
   };

   void PushHitEvent(IEditable* part, unsigned int kind, float scalar);
}
