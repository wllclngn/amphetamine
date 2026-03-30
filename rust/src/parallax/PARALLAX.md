# PARALLAX

*Session-root compositor, display driver replacement, and Dynamic Super Resolution for Linux.*

*"A shift in perception from the same source."*

## What PARALLAX Is

PARALLAX is a Rust binary that owns the display stack. It replaces three things
simultaneously:

1. **explorer.exe** — Wine's desktop message loop and display driver initialization
2. **x11drv / XWayland** — the translation layer between Win32 windows and the real display
3. **Nothing on Linux has DSR** — PARALLAX adds system-wide Dynamic Super Resolution

```
quark     Proton replacement (launcher, environment, DLL deployment)
triskelion      wineserver replacement (protocol 930, ntsync, shared memory)
PARALLAX        explorer.exe replacement (display, input, surface lifecycle, DSR)
```

Three projects. Three layers of the Linux gaming stack rebuilt from scratch.

## What PARALLAX Replaces

In Valve's Proton stack:

```
explorer.exe      creates desktop window, runs message loop, loads display driver
x11drv / XWayland translates Win32 windows to X11, handles input, creates Vulkan surfaces
XWayland          translates X11 protocol to Wayland for the compositor
```

Three translation layers between "game wants to render a frame" and "pixels on screen."

In our stack:

```
triskelion         owns the session, protocol, shared memory, registry
PARALLAX           owns the display, input, Vulkan surface lifecycle, presentation
```

Two components. No translation layers. No explorer.exe. No message loop gate.

## The Problem That Led Here

CSP (Communicating Sequential Processes) analysis revealed that Wine's display driver
initialization gates on a synchronous `SendMessage(WM_NULL)` to the desktop window
(`win32u/driver.c:953`). This message requires a thread with a message loop to receive
it, process it, and reply. Stock Wine uses explorer.exe for this. Without explorer,
the sender blocks forever on a kernel-space ntsync wait for `QS_SMRESULT` that nobody
sets.

The data the display driver actually needs after the WM_NULL returns:

1. Window property `__wine_display_device_guid` on desktop window 0x20 (a GUID atom)
2. Registry key `System\CurrentControlSet\Control\Video\{GUID}\0000`
3. Registry value `GraphicsDriver` at that key (driver .so name)
4. `KeUserModeCallback(NtUserLoadDriver)` loads the named driver .so

Triskelion already pre-populates items 1-3 at daemon startup. The gate exists only
because Wine assumes a desktop message loop. PARALLAX eliminates that assumption
by owning the display information that the gate was protecting.

## The DSR Problem

NVIDIA DSR (Dynamic Super Resolution) on Windows renders at a higher resolution
internally, downscales via GPU, and outputs at native resolution. The monitor never
sees the high-res frame — bandwidth stays the same, refresh rate is preserved.
Text is sharper, edges are smoother, everything looks better on a 1080p panel.

On Linux, this doesn't exist. NVIDIA never ported DSR to their Linux driver
(requested since 2018, no response). X11 had workarounds (`xrandr --scale`),
but on Wayland there is no solution. Nobody has built one.

## Architecture

PARALLAX is a **session root Wayland compositor**. It sits beneath the user's
actual desktop compositor (KDE Plasma, GNOME, Sway, Hyprland) and provides
both game display management and system-wide supersampling transparently.

```
PARALLAX (session root, owns DRM/KMS)
  |
  |-- Enumerates real display hardware
  |      Dell AW2521HF: 1920x1080 @ 240Hz, DP-2
  |      LG SDQHD:      2560x1440 @ 60Hz,  DP-1
  |
  |-- Writes display info to shared memory
  |      triskelion reads this, populates Wine registry
  |      No explorer.exe. No XRandR. No message loop.
  |
  |-- Advertises virtual wl_output per display (DSR mode)
  |      Virtual output 0: 3840x2160 @ 240Hz  (2x multiplier)
  |      Virtual output 1: 5120x2880 @ 60Hz   (2x multiplier)
  |
  |-- Launches nested compositor (desktop mode)
  |      $ kwin_wayland --wayland-display $PARALLAX_DISPLAY
  |      (any Wayland compositor that supports running nested)
  |
  |-- Manages Vulkan surfaces for Wine games
  |      Game renders to PARALLAX-owned surface
  |      No x11drv. No XWayland. No X11.
  |
  |-- Receives composed frames via linux-dmabuf (zero-copy)
  |
  |-- Optional: Vulkan compute shader downscale (DSR)
  |      One dispatch per output per frame, ~1-3ms
  |      Band-limited sampling filter (gamescope quality class)
  |
  |-- DRM atomic commit at native resolution
  |      Monitor sees 1080p @ 240Hz — no bandwidth change
```

### Why this architecture?

**Compositor-agnostic.** Every major Wayland compositor supports running nested.
KWin, Mutter, Sway, Hyprland. PARALLAX doesn't patch any upstream project.

**Distro-agnostic.** Arch, Fedora, Ubuntu, NixOS. Single static binary.

**GPU-vendor-agnostic.** Vulkan compute for the downscale pass. Works on NVIDIA,
AMD, Intel — any GPU with a Vulkan driver.

**Wine-aware.** Unlike gamescope, PARALLAX coordinates with triskelion via shared
memory. Display geometry, input events, and surface lifecycle are managed as a
unified system, not bolted together with X11 hacks.

## Wine's Display Driver Contract

The `user_driver_funcs` vtable contains ~80 function pointers across 11 categories.
Analysis of x11drv (28,692 LOC) and winewayland (7,774 LOC) reveals that for gaming,
the contract collapses to 5 essential capabilities:

### What Games Need (Runtime Hot Path)

| Capability | Driver Functions | Frequency |
|------------|-----------------|-----------|
| Vulkan/GL surface | `VulkanInit`, surface create | Once per swapchain |
| Input events | `ProcessEvents`, keyboard/mouse handlers | Every frame |
| Display geometry | `UpdateDisplayDevices` (add_gpu/source/monitor/modes) | Once at init |
| Cursor control | `SetCursor`, `ClipCursor`, `GetCursorPos`, `SetCursorPos` | Per input event |
| Window lifecycle | `CreateWindow`, `WindowPosChanged`, `ShowWindow`, `DestroyWindow` | Rare at runtime |

### What Games Don't Need

| Category | Functions | Why Irrelevant |
|----------|-----------|----------------|
| GDI rendering | ~60 functions (BitBlt, ExtTextOut, LineTo, etc.) | Games use Vulkan via DXVK |
| Clipboard | ClipboardWindowProc, UpdateClipboard | Not during gameplay |
| Drag-and-drop | Part of clipboard | Not during gameplay |
| System tray | 6 functions | Desktop feature |
| IME | 3 functions | Not for Western games |
| Printing | 5 functions | Never |
| Tablet input | WintabProc | Niche |

The null driver (`nulldrv`) already provides safe no-op implementations for all
non-essential functions. A minimal driver only needs to implement the hot path.

## Display Device Registration

Wine's display driver registers hardware through callbacks in `gdi_device_manager`:

```
add_gpu(name, pci_id, vulkan_uuid)
  Creates: Registry\Machine\System\CurrentControlSet\Enum\PCI\VEN_XXXX&DEV_XXXX
  Stores: GPU name, PCI IDs, Vulkan UUID, LUID, GUID

add_source(connector_name, state_flags, dpi)
  Creates: Registry\Machine\System\CurrentControlSet\Control\Video\{GPU_GUID}\{index}
  Stores: Connector name, primary flag, DPI, symlink to device path

add_monitor(rc_monitor, rc_work, edid, edid_len, hdr_enabled)
  Creates: Registry\Machine\System\CurrentControlSet\Enum\DISPLAY\{monitor_id}
  Stores: Monitor rect, work rect, raw EDID binary, HDR flag

add_modes(current_mode, modes_count, modes[])
  Stores: Array of DEVMODEW (resolution, refresh rate, bit depth, orientation)
  Per source: physical modes, current mode, registry/saved mode
```

All of this data is knowable before any Wine process starts:
- GPU info: `/sys/class/drm/card*/device/` (PCI IDs, driver)
- Connectors: `/sys/class/drm/card*-*/` (status, modes, EDID)
- Vulkan UUID: `vkGetPhysicalDeviceProperties2`
- Current mode: DRM/KMS `drmModeGetCrtc()`

PARALLAX enumerates this at startup, writes it to shared memory. Triskelion reads
it and populates Wine registry keys. No driver callback needed. No explorer. No
XRandR enumeration.

## Triskelion + PARALLAX Handshake

```
                    Shared Memory
                   +-------------+
                   | Display info|  <-- PARALLAX writes: GPU, connectors, modes, EDID
                   | Input state |  <-- PARALLAX writes: cursor pos, button state
                   | Surface fds |  <-- PARALLAX provides: DMA-BUF fds for rendering
                   +-------------+
                     ^         ^
                     |         |
              triskelion     PARALLAX
              (daemon)       (display)
                     |         |
                     v         v
              Wine processes   Real display hardware
              (protocol 930)   (DRM/KMS, Wayland, Vulkan)
```

At startup:
1. PARALLAX enumerates real display hardware (DRM/KMS)
2. PARALLAX writes display info to shared memory segment
3. Triskelion reads display info, populates Wine registry keys
4. Triskelion sets desktop window property with GPU GUID atom
5. Wine processes start, `load_desktop_driver` finds pre-populated data
6. Display driver loads (thin shim reading from PARALLAX shared memory)
7. No WM_NULL gate. No explorer. No message loop.

At runtime:
1. PARALLAX feeds input events to triskelion via shared memory
2. Triskelion injects them into Wine's message queue (`QS_INPUT`)
3. Game creates Vulkan swapchain through PARALLAX driver
4. GPU renders directly to PARALLAX-managed surface
5. PARALLAX presents to real display (DRM atomic commit or Wayland commit)
6. Optional: DSR downscale pass before presentation

## Vulkan Surface Creation

This is the critical path for game rendering.

### How x11drv Does It (Current, Proton)

```
Game calls vkCreateSwapchainKHR
  -> winevulkan translates to host Vulkan
  -> x11drv creates X11 Window via XCreateWindow
  -> vkCreateXlibSurfaceKHR(display, x11_window)
  -> GPU renders to X11 window backing pixmap
  -> X11 compositor presents to display
```

### How winewayland Does It

```
Game calls vkCreateSwapchainKHR
  -> winevulkan translates to host Vulkan
  -> winewayland creates wl_surface via wl_compositor
  -> vkCreateWaylandSurfaceKHR(wl_display, wl_surface)
  -> GPU renders to Wayland surface buffer
  -> Wayland compositor presents to display
```

### How PARALLAX Does It

```
Game calls vkCreateSwapchainKHR
  -> winevulkan translates to host Vulkan
  -> PARALLAX driver creates wl_surface (or DRM surface directly)
  -> vkCreateWaylandSurfaceKHR (or vkCreateDisplayKHR for direct)
  -> GPU renders to PARALLAX-managed surface
  -> PARALLAX presents (with optional DSR downscale)
```

PARALLAX controls the surface lifecycle and knows the display geometry from its
own enumeration, not from asking X11 or the Wayland compositor.

## Input Pipeline

### Current: x11drv (4 hops)

```
Physical input -> kernel evdev -> X server -> X11 events -> x11drv -> Wine message queue
```

### Current: winewayland (3 hops)

```
Physical input -> kernel evdev -> Wayland compositor -> wl_pointer/wl_keyboard -> winewayland -> Wine message queue
```

### PARALLAX Option A: Through Compositor (3 hops)

Same as winewayland. Compositor handles input routing. PARALLAX receives
Wayland input events and converts to Win32 INPUT structs. Most compatible.

### PARALLAX Option B: Direct evdev (2 hops)

```
Physical input -> kernel evdev -> PARALLAX -> triskelion shared memory -> Wine message queue
```

PARALLAX opens evdev devices directly (like gamescope does for the Steam Deck).
Requires input device permissions but eliminates compositor latency. Best for
exclusive fullscreen gaming.

## Prior Art: How Gamescope Does It

Valve solved parts of this for the Steam Deck. Gamescope is a micro-compositor.
Architecture traced from source (`github.com/ValveSoftware/gamescope`):

### Gamescope's pipeline

1. **Fake EDID** (`edid.cpp`) — Patches the real monitor's EDID to advertise
   higher resolutions. Games think they have a 4K display.

2. **Wayland server** — Runs its own Wayland+XWayland server. Games connect to
   this, render at the virtual high resolution.

3. **Vulkan compute shader** (`shaders/cs_composite_blit.comp`, ~50 lines) —
   Samples the high-res texture and writes to a native-res output image. Supports:
   - Band-limited pixel filter (highest quality)
   - Bilinear interpolation
   - Nearest neighbor
   - FSR (AMD FidelityFX Super Resolution)
   - NIS (NVIDIA Image Scaling)

4. **DRM atomic commit** — The native-res composed image gets sent to the display.

### The key function: `vulkan_composite()`

```
rendervulkan.cpp

For each layer (game window, overlays, cursor):
  - Compute scale factors: u_scale = (outputWidth/inputWidth, outputHeight/inputHeight)
  - Bind source texture (game's framebuffer)
  - Bind output image (native-res render target)
  - Dispatch compute shader: div_roundup(outputWidth, 8) x div_roundup(outputHeight, 8)

The shader (cs_composite_blit.comp):
  - 8x8 workgroups
  - Each invocation maps one output pixel to source coordinates via u_scale
  - Samples with configurable filter
  - Blends layers with per-layer opacity
  - Writes to output image
```

### What gamescope carries that we don't need

~15,000 lines of Steam Deck-specific logic:
- Steam overlay handling, game window management
- FSR/NIS upscaling (we're downscaling, opposite direction)
- HDR tone mapping, color management
- XWayland sandboxing per game
- Input emulation, IME support
- PipeWire screen capture

PARALLAX needs the core ~200 lines of scaling logic, minus the bloat.

## Prior Art: How KWin Could Do It (But Doesn't)

KWin's rendering pipeline traced from source (`invent.kde.org/plasma/kwin`):

```
Compositor::composite()                            compositor.cpp:585
  -> prepareRendering()                            compositor.cpp:405
    -> mapGlobalLogicalToOutputDeviceCoordinates() compositor.cpp:345
      -> Sets targetRect from scale * logical size
  -> EglGbmLayer::doBeginFrame()                   drm_egl_layer.cpp:52
    -> startRendering(targetRect().size())          <-- BUFFER SIZE DECIDED HERE
      -> EglGbmLayerSurface creates swapchain at that size
        -> GLFramebuffer wraps the GBM buffer
          -> Scene renders into it
            -> DRM atomic commit presents it
```

Buffer size flows from one place: `targetRect().size()`. A KWin patch would multiply
by a render factor and add a `glBlitFramebuffer` downscale before DRM presentation.

We chose not to patch KWin because:
- Only works for KDE, not GNOME/Sway/Hyprland
- Requires maintaining a fork or getting it upstream (slow)
- A standalone compositor is more useful to the Linux ecosystem

## What Winewayland Got Right

- Client surface abstraction (decouples Vulkan/GL from window management)
- Role-less wl_surface initialization (avoids compositor spam for hidden windows)
- Serial-based configuration handshake (Wayland correctness)
- Per-object event queues for thread safety
- Coordinate helper functions for DPI conversion

## What Winewayland Got Wrong (For Our Purposes)

- Still a Wine display driver — 7,774 lines translating Win32 to Wayland
- Still requires explorer.exe and the desktop message loop
- Maps Windows window hierarchy to Wayland surfaces (topological mismatch)
- Coordinate conversions in hot paths
- Single-seat assumption
- Incomplete: 7 VK codes unmapped, modifier state not synced, DPI breaks constraints

Same problem as Etaash's patches: bolting Wayland support onto Wine's existing driver
model. The right approach if you accept Wine's assumptions. The wrong approach if you
don't.

Our ntsync precedent: don't patch wineserver's synchronization — replace it with
kernel primitives. Same logic applies to display: don't patch Wine's driver model
to speak Wayland — own the display directly.

## Design

### Modules

```
parallax/
  src/
    main.rs           -- CLI parsing, config loading, session startup
    compositor.rs     -- Wayland compositor: wl_output, wl_surface, linux-dmabuf
    output.rs         -- DRM/KMS output enumeration and mode setting
    scaler.rs         -- Vulkan compute pipeline: shader dispatch, buffer mgmt
    display_info.rs   -- Shared memory writer: GPU, connectors, modes for triskelion
    input.rs          -- Input routing: Wayland events or direct evdev
    config.rs         -- TOML config: per-output multiplier, filter, child cmd
    shader.glsl       -- The downscale compute shader (~50 lines)
```

### Config

```toml
# ~/.config/parallax/parallax.toml

# Default render multiplier (1.0 = passthrough, 2.0 = DSR)
multiplier = 1.0

# Downscale filter (only used when multiplier > 1.0): "lanczos", "bilinear", "nearest"
filter = "lanczos"

# Child compositor command (what PARALLAX launches nested for desktop)
child = "kwin_wayland"

# Triskelion shared memory path (auto-detected if not set)
# triskelion_shm = "/triskelion-XXXXXXXX"

# Input mode: "compositor" (through Wayland) or "evdev" (direct, lower latency)
input_mode = "compositor"

# Per-output overrides (matched by connector name or EDID model)
[[output]]
match = "AW2521HF"       # Dell Alienware 1080p 240Hz
multiplier = 2.0          # Render at 3840x2160, output 1920x1080

[[output]]
match = "LG SDQHD"
multiplier = 1.0          # Already high-res, no supersampling needed
```

### Compute Shader (DSR)

```glsl
// shader.glsl -- PARALLAX Lanczos-2 downscale compute shader
#version 450

layout(local_size_x = 8, local_size_y = 8, local_size_z = 1) in;

layout(set = 0, binding = 0) uniform sampler2D src;
layout(set = 0, binding = 1, rgba8) uniform writeonly image2D dst;

layout(push_constant) uniform PushConstants {
    vec2 scale;    // output_size / input_size
    vec2 src_size; // input texture dimensions
};

float lanczos(float x) {
    if (x == 0.0) return 1.0;
    if (abs(x) >= 2.0) return 0.0;
    float pi_x = x * 3.14159265;
    return (sin(pi_x) / pi_x) * (sin(pi_x * 0.5) / (pi_x * 0.5));
}

vec4 sample_lanczos(vec2 center) {
    vec4 color = vec4(0.0);
    float weight_sum = 0.0;
    for (int y = -1; y <= 2; y++) {
        for (int x = -1; x <= 2; x++) {
            vec2 sample_pos = floor(center) + 0.5 + vec2(float(x), float(y));
            vec2 dist = center - sample_pos;
            float w = lanczos(dist.x) * lanczos(dist.y);
            color += texture(src, sample_pos / src_size) * w;
            weight_sum += w;
        }
    }
    return color / weight_sum;
}

void main() {
    uvec2 coord = gl_GlobalInvocationID.xy;
    uvec2 out_size = imageSize(dst);
    if (coord.x >= out_size.x || coord.y >= out_size.y) return;
    vec2 src_coord = (vec2(coord) + 0.5) / scale;
    imageStore(dst, ivec2(coord), sample_lanczos(src_coord));
}
```

### Crate Stack

```toml
[package]
name = "parallax"
version = "0.1.0"
edition = "2024"

[dependencies]
smithay = { version = "0.4", features = ["backend_drm", "backend_gbm", "wayland_frontend"] }
ash = "0.38"
gpu-allocator = "0.27"
drm = "0.14"
gbm = "0.17"
serde = { version = "1", features = ["derive"] }
toml = "0.8"
libc = "0.2"

[profile.release]
opt-level = 3
lto = "fat"
codegen-units = 1
strip = "symbols"
```

### Frame Loop (Pseudocode)

```rust
fn frame_loop(state: &mut ParallaxState) {
    // 1. Wait for game/compositor to submit a frame
    //    (wl_surface.commit with linux-dmabuf buffer attached)
    let dma_buf = state.wayland.recv_frame();

    // 2. Import DMA-BUF as Vulkan image (zero-copy -- same GPU memory)
    let vk_src = state.vulkan.import_dmabuf(dma_buf);

    // 3. Optional DSR: dispatch compute shader high-res -> native-res
    let vk_dst = if state.config.multiplier > 1.0 {
        state.vulkan.downscale(vk_src, &state.output)
    } else {
        vk_src // passthrough, no downscale
    };

    // 4. Export as DRM framebuffer
    let drm_fb = state.drm.import_vulkan_image(vk_dst);

    // 5. DRM atomic commit -- present to real display at native res
    state.drm.atomic_commit(drm_fb);
}
```

## Performance Budget

| Stage | Cost | Notes |
|-------|------|-------|
| DMA-BUF import | ~0 | Zero-copy, same GPU memory |
| Vulkan compute dispatch | 1-3ms | Only when DSR multiplier > 1.0 |
| DRM atomic commit | <1ms | Kernel-side, non-blocking with async |
| **Total overhead (DSR)** | **~2-4ms** | **Leaves 240Hz headroom (4.16ms/frame)** |
| **Total overhead (passthrough)** | **<1ms** | **DMA-BUF import + commit only** |

At 240Hz, each frame has 4.16ms. With DSR enabled, gamescope runs a similar
pipeline on the Steam Deck's much weaker APU at 60-90Hz without issue. On an
RTX 3070, the compute dispatch will be well under 2ms.

## Usage

```bash
# Launch KDE Plasma with PARALLAX (passthrough, no DSR)
parallax -- kwin_wayland

# Launch with 2x DSR on all outputs
parallax --multiplier 2.0 -- kwin_wayland

# Launch Sway
parallax -- sway

# Custom config
parallax --config ~/.config/parallax/parallax.toml -- kwin_wayland

# Game launch via quark (triskelion + PARALLAX coordinate automatically)
# PARALLAX is already running as session root; triskelion reads its shared memory
```

### Display Manager Integration

```ini
# /usr/share/wayland-sessions/parallax-plasma.desktop
[Desktop Entry]
Name=Plasma (PARALLAX)
Comment=KDE Plasma with DSR via PARALLAX
Exec=parallax -- kwin_wayland
Type=Application
```

## Core Philosophy: System-Native Gaming

The fundamental problem with Proton is that Steam dictates the display stack.
Your system runs Wayland. Your compositor is KDE Plasma. Your GPU driver is
NVIDIA 570. None of that matters — Proton forces XWayland, x11drv, and its
own compositor assumptions onto your system.

PARALLAX inverts this. The game adapts to the system, not the other way around.

**Wine 11.5 already ships winewayland.** It's 7,774 lines of working code in
upstream Wine — not Proton's fork, not Valve's patches. It creates `wl_surface`
objects, handles `VkWaylandSurfaceKHR`, does display enumeration through native
Wayland protocols. The machinery exists.

The base case doesn't need PARALLAX at all:

```
triskelion sets GraphicsDriver = "winewayland.drv"
  -> Wine loads winewayland from the system Wine installation
  -> winewayland talks to the user's native Wayland compositor
  -> Game renders through the user's display stack
  -> No XWayland. No x11drv. No Proton display layer.
```

PARALLAX becomes an enhancement layer on top of this:
- DSR (render high, downscale to native)
- Direct input routing (evdev bypass for lower latency)
- Display info pre-population (eliminate explorer.exe entirely)
- Session-root compositor mode (own DRM/KMS for maximum control)

The user's system drives the experience. PARALLAX extends it. Neither replaces it.

### Phased Approach

**Phase 0 (now):** Fix the WM_NULL sent-message gate in triskelion. Get x11drv
loading and games rendering through the existing XWayland path. Proves the daemon
works end-to-end.

**Phase 1:** Switch `GraphicsDriver` from `winex11.drv` to `winewayland.drv`.
Test with the user's native Wayland compositor. Eliminate XWayland entirely.
May require triskelion to pre-populate additional display registry keys that
winewayland's `UpdateDisplayDevices` expects.

**Phase 2:** Build PARALLAX as a display info daemon. It enumerates hardware
via DRM/KMS, writes display geometry to shared memory, triskelion reads it
and populates registry. Eliminates explorer.exe and the WM_NULL gate entirely.
winewayland still does the actual rendering.

**Phase 3:** PARALLAX as session-root compositor with DSR. Owns DRM/KMS,
advertises virtual outputs at higher resolution, does Vulkan compute downscale.
Full architecture as designed above.

Each phase is independently useful. Each phase works without the next.

## Open Questions

### Driver Strategy

Do we:
A. Write a minimal Wine display driver (`parallaxdrv`) that reads from shared memory?
B. Eliminate the driver entirely by satisfying Wine's expectations at registry/shared memory level?
C. Use `nulldrv` with targeted overrides for Vulkan surface creation and input?

Option A is most compatible. Option B is most radical. Option C is fastest to prototype.

### Surface Ownership

Who creates the Vulkan surface?
- PARALLAX (owns display, creates surface, passes fd to Wine)
- Wine's winevulkan (creates surface using PARALLAX-provided wl_display/wl_surface)
- Direct DRM (bypass Wayland entirely for exclusive fullscreen)

### Input Routing

Do we:
- Go through the Wayland compositor (compatible, higher latency)
- Read evdev directly (lower latency, requires permissions)
- Support both with a config switch

### Compositor Relationship

Is PARALLAX:
- A session-root compositor (owns DRM/KMS, launches nested compositor)
- A display management daemon (coordinates with existing compositor)
- Both, depending on configuration

## Research Needed

### Resolved

- **Kernel video drivers (DRM/KMS, GBM, mesa):** NOT replacing these. We're
  consumers, not replacers. Every path (x11drv, winewayland, gamescope, PARALLAX)
  bottoms out at the same kernel interfaces. Solved problem.

- **DRM/KMS direct rendering (VK_KHR_display):** Investigated, not pursuing for
  base case. Exclusive fullscreen only, incompatible with desktop compositors.
  Could be a Phase 3 option for PARALLAX session-root mode.

- **winewayland capabilities:** Confirmed present in Wine 11.5. 7,774 LOC,
  all 20 core driver functions implemented. Vulkan surface creation works.
  CachyOS extends to 11,062 LOC with OpenGL, fractional-scale, color management.

### Still Needed

1. **Wine's winevulkan layer**: How does it intercept Vulkan calls? Can we
   provide a custom `VkSurfaceKHR` without a traditional display driver?
   Needed for Phase 3 (PARALLAX-owned surfaces).

2. **Shared memory input injection**: Can triskelion inject input events into
   Wine's message queue from shared memory without a display driver's
   `ProcessEvents`? Needed for Phase 2+ (eliminating explorer.exe).

3. **Gamescope internals**: How does gamescope handle input, surface management,
   and display enumeration? Closest architectural precedent for Phase 3.

4. **Wine's session shared memory layout**: What fields does Wine read from
   `queue_shm_t`, `window_shm_t`, `input_shm_t` at runtime? Needed for
   Phase 2 (pre-populating display info without a driver).

5. **Wayland protocols for game rendering**: wl_compositor, xdg_shell,
   linux-dmabuf-v1, wp-pointer-constraints-v1, wp-relative-pointer-v1.
   Which are mandatory? Needed for Phase 1 (winewayland validation).

## Status

### Research Complete
- [x] Problem identified (WM_NULL desktop gate via CSP analysis)
- [x] Wine display driver contract mapped (80 functions, 5 essential for games)
- [x] x11drv runtime behavior traced (28,692 LOC analyzed)
- [x] winewayland design analyzed (7,774 LOC, strengths/weaknesses catalogued)
- [x] Display device registration path traced (add_gpu/source/monitor/modes)
- [x] Triskelion pre-populates registry keys and desktop window property
- [x] QS_SMRESULT fix unblocks sent-message wait loop
- [x] Gamescope scaling pipeline traced and understood
- [x] KWin rendering pipeline traced (injection points identified)
- [x] Compute shader written (Lanczos-2 downscale)

### Design In Progress
- [ ] Driver strategy decision (parallaxdrv vs nulldrv vs no driver)
- [ ] Vulkan surface creation path design
- [ ] Input pipeline design
- [ ] Shared memory contract between triskelion and PARALLAX

### Implementation
- [ ] DRM/KMS output enumeration and mode setting
- [ ] Shared memory writer (display info for triskelion)
- [ ] Wayland compositor skeleton (smithay or raw wayland-server)
- [ ] Vulkan compute pipeline (ash)
- [ ] DMA-BUF import/export (linux-dmabuf protocol)
- [ ] Frame loop integration
- [ ] Input routing (Wayland events and/or direct evdev)
- [ ] Multi-output support
- [ ] Config loading (TOML)
- [ ] Prototype: game renders a frame through the new stack
- [ ] DSR integration
- [ ] Testing on real hardware
