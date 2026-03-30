// DRM/KMS output enumeration
//
// Enumerates real display hardware via DRM ioctls:
// - GPUs (cards) with PCI IDs and driver name
// - Connectors with status, type, EDID
// - Modes (resolution, refresh rate) per connector
//
// This data feeds the shared memory segment that triskelion reads
// to populate Wine's display registry keys.

use std::fs;
use std::path::{Path, PathBuf};
use std::os::unix::io::RawFd;

#[derive(Debug, Clone)]
pub struct GpuInfo {
    pub driver: String,
    pub pci_vendor: u32,
    pub pci_device: u32,
    pub pci_subsys_vendor: u32,
    pub pci_subsys_device: u32,
    pub pci_revision: u32,
    pub pci_bus_id: String,
    pub gpu_name: String,    // human-readable, e.g. "NVIDIA GeForce RTX 3070 Ti"
    pub vram_bytes: u64,     // video memory in bytes (0 = unknown)
}

#[derive(Debug, Clone)]
pub struct ConnectorInfo {
    pub name: String,
    pub connector_id: u32,
    pub connected: bool,
    pub connector_type: u32,
    pub connector_type_id: u32,
    pub mm_width: u32,
    pub mm_height: u32,
    pub edid: Vec<u8>,
    pub modes: Vec<ModeInfo>,
    pub current_mode: Option<ModeInfo>,
}

#[derive(Debug, Clone, Copy)]
pub struct ModeInfo {
    pub width: u32,
    pub height: u32,
    pub refresh: u32,
    pub flags: u32,
    pub mode_type: u32,
}

impl ModeInfo {
    pub fn is_preferred(&self) -> bool {
        self.mode_type & DRM_MODE_TYPE_PREFERRED != 0
    }
}

const DRM_MODE_TYPE_PREFERRED: u32 = 1 << 3;

#[derive(Debug)]
pub struct DisplayHardware {
    pub gpus: Vec<GpuInfo>,
    pub connectors: Vec<ConnectorInfo>,
}

pub fn enumerate() -> DisplayHardware {
    let mut hw = DisplayHardware {
        gpus: Vec::new(),
        connectors: Vec::new(),
    };

    // Find DRM card devices
    let cards = find_drm_cards();
    for card_path in &cards {
        if let Some(gpu) = read_gpu_info(card_path) {
            hw.gpus.push(gpu);
        }

        let fd = match open_drm_device(card_path) {
            Some(fd) => fd,
            None => continue,
        };

        if let Some(res) = get_drm_resources(fd) {
            for conn_id in &res.connector_ids {
                if let Some(conn) = get_connector_info(fd, *conn_id, card_path) {
                    hw.connectors.push(conn);
                }
            }
        }

        unsafe { libc::close(fd); }
    }

    hw
}

fn find_drm_cards() -> Vec<PathBuf> {
    let mut cards = Vec::new();
    if let Ok(entries) = fs::read_dir("/dev/dri") {
        for entry in entries.flatten() {
            let name = entry.file_name();
            let name_str = name.to_string_lossy();
            if name_str.starts_with("card") && !name_str.contains("render") {
                cards.push(entry.path());
            }
        }
    }
    cards.sort();
    cards
}

fn read_gpu_info(card_path: &Path) -> Option<GpuInfo> {
    let card_name = card_path.file_name()?.to_str()?;
    let sys_path = PathBuf::from(format!("/sys/class/drm/{card_name}/device"));

    let driver = fs::read_link(sys_path.join("driver"))
        .ok()
        .and_then(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()))
        .unwrap_or_default();

    let pci_vendor = read_sysfs_hex(&sys_path.join("vendor"));
    let pci_device = read_sysfs_hex(&sys_path.join("device"));
    let pci_subsys_vendor = read_sysfs_hex(&sys_path.join("subsystem_vendor"));
    let pci_subsys_device = read_sysfs_hex(&sys_path.join("subsystem_device"));
    let pci_revision = read_sysfs_hex(&sys_path.join("revision"));

    let pci_bus_id = fs::read_link(sys_path.join("driver"))
        .ok()
        .and_then(|_| {
            // Bus ID from uevent
            let uevent = fs::read_to_string(sys_path.join("uevent")).ok()?;
            for line in uevent.lines() {
                if let Some(id) = line.strip_prefix("PCI_SLOT_NAME=") {
                    return Some(id.to_string());
                }
            }
            None
        })
        .unwrap_or_default();

    let gpu_name = detect_gpu_name(&card_path, &pci_bus_id);
    let vram_bytes = detect_vram(&card_path);

    Some(GpuInfo {
        driver,
        pci_vendor,
        pci_device,
        pci_subsys_vendor,
        pci_subsys_device,
        pci_revision,
        pci_bus_id,
        gpu_name,
        vram_bytes,
    })
}

fn detect_gpu_name(card_path: &Path, pci_bus_id: &str) -> String {
    // NVIDIA: /proc/driver/nvidia/gpus/<bus_id>/information → "Model: ..."
    if !pci_bus_id.is_empty() {
        let info_path = format!("/proc/driver/nvidia/gpus/{pci_bus_id}/information");
        if let Ok(info) = fs::read_to_string(&info_path) {
            for line in info.lines() {
                if line.starts_with("Model:") {
                    return line.trim_start_matches("Model:").trim().to_string();
                }
            }
        }
    }
    // Any NVIDIA GPU
    if let Ok(entries) = fs::read_dir("/proc/driver/nvidia/gpus/") {
        for entry in entries.flatten() {
            if let Ok(info) = fs::read_to_string(entry.path().join("information")) {
                for line in info.lines() {
                    if line.starts_with("Model:") {
                        return line.trim_start_matches("Model:").trim().to_string();
                    }
                }
            }
        }
    }
    // lspci fallback
    if let Ok(output) = std::process::Command::new("lspci").output() {
        let stdout = String::from_utf8_lossy(&output.stdout);
        for line in stdout.lines() {
            if line.contains("VGA") || line.contains("3D controller") {
                if let Some(desc) = line.split(": ").nth(1) {
                    return desc.to_string();
                }
            }
        }
    }
    let _ = card_path; // suppress unused warning
    "Wine Display Adapter".to_string()
}

fn detect_vram(card_path: &Path) -> u64 {
    // AMD: mem_info_vram_total (bytes)
    let vram_path = card_path.join("device/mem_info_vram_total");
    if let Ok(s) = fs::read_to_string(&vram_path) {
        if let Ok(bytes) = s.trim().parse::<u64>() {
            return bytes;
        }
    }
    // NVIDIA: nvidia-smi reports MB
    if let Ok(output) = std::process::Command::new("nvidia-smi")
        .args(["--query-gpu=memory.total", "--format=csv,noheader,nounits"])
        .output()
    {
        if output.status.success() {
            if let Ok(s) = String::from_utf8(output.stdout) {
                if let Ok(mb) = s.trim().parse::<u64>() {
                    return mb * 1024 * 1024;
                }
            }
        }
    }
    0
}

fn read_sysfs_hex(path: &Path) -> u32 {
    fs::read_to_string(path)
        .ok()
        .and_then(|s| {
            let s = s.trim().trim_start_matches("0x");
            u32::from_str_radix(s, 16).ok()
        })
        .unwrap_or(0)
}

fn open_drm_device(card_path: &Path) -> Option<RawFd> {
    let c_path = std::ffi::CString::new(card_path.to_str()?).ok()?;
    let fd = unsafe { libc::open(c_path.as_ptr(), libc::O_RDWR | libc::O_CLOEXEC) };
    if fd < 0 {
        // Try read-only for enumeration
        let fd = unsafe { libc::open(c_path.as_ptr(), libc::O_RDONLY | libc::O_CLOEXEC) };
        if fd < 0 { return None; }
        return Some(fd);
    }
    Some(fd)
}

// DRM ioctl structures (matching linux/drm.h and drm_mode.h)

const DRM_IOCTL_BASE: u64 = b'd' as u64;

const fn drm_iowr(nr: u64, size: u64) -> u64 {
    // _IOWR('d', nr, type) = direction(3) << 30 | size << 16 | type << 8 | nr
    (3 << 30) | (size << 16) | (DRM_IOCTL_BASE << 8) | nr
}

const DRM_IOCTL_MODE_GETRESOURCES: u64 = drm_iowr(0xA0, 64);
const DRM_IOCTL_MODE_GETCONNECTOR: u64 = drm_iowr(0xA7, 80);
const DRM_IOCTL_MODE_GETCRTC: u64 = drm_iowr(0xA1, 72);

#[repr(C)]
struct DrmModeResources {
    fb_id_ptr: u64,
    crtc_id_ptr: u64,
    connector_id_ptr: u64,
    encoder_id_ptr: u64,
    count_fbs: u32,
    count_crtcs: u32,
    count_connectors: u32,
    count_encoders: u32,
    min_width: u32,
    max_width: u32,
    min_height: u32,
    max_height: u32,
}

#[repr(C)]
#[derive(Default)]
struct DrmModeGetConnector {
    encoders_ptr: u64,
    modes_ptr: u64,
    props_ptr: u64,
    prop_values_ptr: u64,
    count_modes: u32,
    count_props: u32,
    count_encoders: u32,
    encoder_id: u32,
    connector_id: u32,
    connector_type: u32,
    connector_type_id: u32,
    connection: u32,
    mm_width: u32,
    mm_height: u32,
    subpixel: u32,
    _pad: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct DrmModeModeInfo {
    clock: u32,
    hdisplay: u16,
    hsync_start: u16,
    hsync_end: u16,
    htotal: u16,
    hskew: u16,
    vdisplay: u16,
    vsync_start: u16,
    vsync_end: u16,
    vtotal: u16,
    vscan: u16,
    vrefresh: u32,
    flags: u32,
    mode_type: u32,
    name: [u8; 32],
}

#[repr(C)]
#[derive(Default)]
struct DrmModeGetCrtc {
    set_connectors_ptr: u64,
    count_connectors: u32,
    crtc_id: u32,
    fb_id: u32,
    x: u32,
    y: u32,
    gamma_size: u32,
    mode_valid: u32,
    mode: DrmModeModeInfo,
}

struct DrmResources {
    connector_ids: Vec<u32>,
}

fn get_drm_resources(fd: RawFd) -> Option<DrmResources> {
    // First call: get counts
    let mut res: DrmModeResources = unsafe { std::mem::zeroed() };
    let ret = unsafe { libc::ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &mut res as *mut _) };
    if ret < 0 { return None; }

    let mut connector_ids = vec![0u32; res.count_connectors as usize];
    let mut _crtc_ids = vec![0u32; res.count_crtcs as usize];

    // Second call: fill arrays
    res.connector_id_ptr = connector_ids.as_mut_ptr() as u64;
    res.crtc_id_ptr = _crtc_ids.as_mut_ptr() as u64;
    // Need to provide fb and encoder arrays too
    let mut fb_ids = vec![0u32; res.count_fbs as usize];
    let mut encoder_ids = vec![0u32; res.count_encoders as usize];
    res.fb_id_ptr = fb_ids.as_mut_ptr() as u64;
    res.encoder_id_ptr = encoder_ids.as_mut_ptr() as u64;

    let ret = unsafe { libc::ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &mut res as *mut _) };
    if ret < 0 { return None; }

    Some(DrmResources { connector_ids })
}

fn get_connector_info(fd: RawFd, connector_id: u32, card_path: &Path) -> Option<ConnectorInfo> {
    // First call: get counts
    let mut conn = DrmModeGetConnector::default();
    conn.connector_id = connector_id;

    let ret = unsafe { libc::ioctl(fd, DRM_IOCTL_MODE_GETCONNECTOR, &mut conn as *mut _) };
    if ret < 0 { return None; }

    let connected = conn.connection == 1; // DRM_MODE_CONNECTED

    // Second call: get modes
    let mut drm_modes = vec![DrmModeModeInfo::default(); conn.count_modes as usize];
    let mut encoders = vec![0u32; conn.count_encoders as usize];
    let mut props = vec![0u32; conn.count_props as usize];
    let mut prop_values = vec![0u64; conn.count_props as usize];

    conn.modes_ptr = drm_modes.as_mut_ptr() as u64;
    conn.encoders_ptr = encoders.as_mut_ptr() as u64;
    conn.props_ptr = props.as_mut_ptr() as u64;
    conn.prop_values_ptr = prop_values.as_mut_ptr() as u64;

    let ret = unsafe { libc::ioctl(fd, DRM_IOCTL_MODE_GETCONNECTOR, &mut conn as *mut _) };
    if ret < 0 { return None; }

    let modes: Vec<ModeInfo> = drm_modes.iter().map(|m| ModeInfo {
        width: m.hdisplay as u32,
        height: m.vdisplay as u32,
        refresh: if m.vrefresh != 0 { m.vrefresh } else { calculate_refresh(m) },
        flags: m.flags,
        mode_type: m.mode_type,
    }).collect();

    let connector_type_name = connector_type_str(conn.connector_type);
    let name = format!("{}-{}", connector_type_name, conn.connector_type_id);

    // Read EDID from sysfs
    let edid = read_edid(card_path, conn.connector_type, conn.connector_type_id);

    // Find current mode via CRTC
    let current_mode = if connected && conn.encoder_id != 0 {
        find_current_mode(fd, conn.encoder_id)
    } else {
        None
    };

    Some(ConnectorInfo {
        name,
        connector_id,
        connected,
        connector_type: conn.connector_type,
        connector_type_id: conn.connector_type_id,
        mm_width: conn.mm_width,
        mm_height: conn.mm_height,
        edid,
        modes,
        current_mode,
    })
}

fn calculate_refresh(m: &DrmModeModeInfo) -> u32 {
    if m.htotal == 0 || m.vtotal == 0 { return 0; }
    let num = m.clock as u64 * 1000;
    let den = m.htotal as u64 * m.vtotal as u64;
    if den == 0 { return 0; }
    ((num + den / 2) / den) as u32
}

fn find_current_mode(fd: RawFd, encoder_id: u32) -> Option<ModeInfo> {
    // Get encoder to find CRTC
    #[repr(C)]
    struct DrmModeGetEncoder {
        encoder_id: u32,
        encoder_type: u32,
        crtc_id: u32,
        possible_crtcs: u32,
        possible_clones: u32,
    }
    const DRM_IOCTL_MODE_GETENCODER: u64 = drm_iowr(0xA6, 20);

    let mut enc = DrmModeGetEncoder {
        encoder_id,
        encoder_type: 0,
        crtc_id: 0,
        possible_crtcs: 0,
        possible_clones: 0,
    };
    let ret = unsafe { libc::ioctl(fd, DRM_IOCTL_MODE_GETENCODER, &mut enc as *mut _) };
    if ret < 0 || enc.crtc_id == 0 { return None; }

    let mut crtc = DrmModeGetCrtc::default();
    crtc.crtc_id = enc.crtc_id;
    let ret = unsafe { libc::ioctl(fd, DRM_IOCTL_MODE_GETCRTC, &mut crtc as *mut _) };
    if ret < 0 || crtc.mode_valid == 0 { return None; }

    let m = &crtc.mode;
    Some(ModeInfo {
        width: m.hdisplay as u32,
        height: m.vdisplay as u32,
        refresh: if m.vrefresh != 0 { m.vrefresh } else { calculate_refresh(m) },
        flags: m.flags,
        mode_type: m.mode_type,
    })
}

fn read_edid(card_path: &Path, connector_type: u32, type_id: u32) -> Vec<u8> {
    let card_name = match card_path.file_name().and_then(|n| n.to_str()) {
        Some(n) => n,
        None => return Vec::new(),
    };
    // Kernel sysfs uses short connector names (DP, HDMI-A, etc.)
    let sysfs_type = connector_type_sysfs(connector_type);
    let edid_path = format!("/sys/class/drm/{card_name}-{sysfs_type}-{type_id}/edid");
    fs::read(&edid_path).unwrap_or_default()
}

fn connector_type_str(t: u32) -> &'static str {
    match t {
        0 => "Unknown",
        1 => "VGA",
        2 => "DVI-I",
        3 => "DVI-D",
        4 => "DVI-A",
        5 => "Composite",
        6 => "SVIDEO",
        7 => "LVDS",
        8 => "Component",
        9 => "9PinDIN",
        10 => "DP",
        11 => "HDMI-A",
        12 => "HDMI-B",
        13 => "TV",
        14 => "eDP",
        15 => "VIRTUAL",
        16 => "DSI",
        17 => "DPI",
        18 => "WRITEBACK",
        19 => "SPI",
        20 => "USB",
        _ => "Unknown",
    }
}

// Sysfs connector type names (kernel uses these in /sys/class/drm/)
fn connector_type_sysfs(t: u32) -> &'static str {
    connector_type_str(t)
}

// Parse EDID to extract monitor name
pub fn edid_monitor_name(edid: &[u8]) -> Option<String> {
    if edid.len() < 128 { return None; }
    // Descriptor blocks start at offset 54, 4 blocks of 18 bytes each
    for i in 0..4 {
        let base = 54 + i * 18;
        if base + 18 > edid.len() { break; }
        // Monitor name descriptor: bytes 0-2 = 0x00, byte 3 = 0xFC
        if edid[base] == 0 && edid[base + 1] == 0 && edid[base + 2] == 0 && edid[base + 3] == 0xFC {
            let name_bytes = &edid[base + 5..base + 18];
            let name = name_bytes.iter()
                .take_while(|&&b| b != 0x0A && b != 0x00)
                .map(|&b| b as char)
                .collect::<String>();
            return Some(name.trim().to_string());
        }
    }
    None
}

// Parse EDID manufacturer ID (3-letter code from bytes 8-9)
pub fn edid_manufacturer(edid: &[u8]) -> Option<String> {
    if edid.len() < 10 { return None; }
    let b0 = edid[8] as u16;
    let b1 = edid[9] as u16;
    let combined = (b0 << 8) | b1;
    let c1 = ((combined >> 10) & 0x1F) as u8 + b'A' - 1;
    let c2 = ((combined >> 5) & 0x1F) as u8 + b'A' - 1;
    let c3 = (combined & 0x1F) as u8 + b'A' - 1;
    if c1.is_ascii_uppercase() && c2.is_ascii_uppercase() && c3.is_ascii_uppercase() {
        Some(format!("{}{}{}", c1 as char, c2 as char, c3 as char))
    } else {
        None
    }
}
