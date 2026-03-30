// PE Import Scanner — reads a Windows executable's import table to determine
// what graphics API and runtime DLLs it needs. Zero-copy, direct syscall I/O.

use std::path::Path;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum RenderApi {
    Vulkan,      // imports vulkan-1.dll
    DirectX12,   // imports d3d12.dll
    DirectX11,   // imports d3d11.dll or d3d10*.dll
    DirectX9,    // imports d3d9.dll
    DirectX8,    // imports d3d8.dll or ddraw.dll
    OpenGL,      // imports opengl32.dll
    None,        // no graphics imports detected
}

#[derive(Debug)]
pub struct PeScanResult {
    pub render_api: RenderApi,
    pub machine: u16,          // IMAGE_FILE_MACHINE_*
    pub imports: Vec<String>,  // all imported DLL names (lowercase)
    pub needs_steam_api: bool, // imports steam_api64.dll or steam_api.dll
    pub needs_nvapi: bool,     // imports nvapi64.dll or nvapi.dll
    pub needs_xinput: bool,    // imports xinput*.dll
    pub needs_xaudio: bool,   // imports xaudio*.dll
}

pub fn scan_pe(exe_path: &Path) -> Option<PeScanResult> {
    let fd = unsafe {
        libc::open(
            std::ffi::CString::new(exe_path.to_str()?).ok()?.as_ptr(),
            libc::O_RDONLY | libc::O_CLOEXEC,
        )
    };
    if fd < 0 { return None; }

    // Read DOS header (64 bytes)
    let mut dos = [0u8; 64];
    let n = unsafe { libc::pread(fd, dos.as_mut_ptr() as *mut _, 64, 0) };
    if n != 64 || dos[0] != b'M' || dos[1] != b'Z' {
        unsafe { libc::close(fd); }
        return None;
    }

    // e_lfanew at offset 0x3C (u32 LE)
    let pe_offset = u32::from_le_bytes([dos[0x3C], dos[0x3D], dos[0x3E], dos[0x3F]]) as i64;

    // Read PE signature + COFF header + optional header start (256 bytes)
    let mut pe_buf = [0u8; 256];
    let n = unsafe { libc::pread(fd, pe_buf.as_mut_ptr() as *mut _, 256, pe_offset) };
    if n < 120 || pe_buf[0] != b'P' || pe_buf[1] != b'E' || pe_buf[2] != 0 || pe_buf[3] != 0 {
        unsafe { libc::close(fd); }
        return None;
    }

    // COFF header starts at pe_offset+4
    let machine = u16::from_le_bytes([pe_buf[4], pe_buf[5]]);
    let num_sections = u16::from_le_bytes([pe_buf[6], pe_buf[7]]);
    let optional_hdr_size = u16::from_le_bytes([pe_buf[20], pe_buf[21]]) as usize;

    // Optional header starts at pe_offset+24
    let opt_magic = u16::from_le_bytes([pe_buf[24], pe_buf[25]]);
    let is_pe32plus = opt_magic == 0x20B; // PE32+ (64-bit)

    // Import directory RVA + size
    let import_dir_offset = if is_pe32plus { 24 + 120 } else { 24 + 104 }; // offset within pe_buf
    if import_dir_offset + 8 > pe_buf.len() {
        unsafe { libc::close(fd); }
        return None;
    }
    let import_rva = u32::from_le_bytes([
        pe_buf[import_dir_offset], pe_buf[import_dir_offset + 1],
        pe_buf[import_dir_offset + 2], pe_buf[import_dir_offset + 3],
    ]);
    let _import_size = u32::from_le_bytes([
        pe_buf[import_dir_offset + 4], pe_buf[import_dir_offset + 5],
        pe_buf[import_dir_offset + 6], pe_buf[import_dir_offset + 7],
    ]);

    if import_rva == 0 {
        unsafe { libc::close(fd); }
        return Some(PeScanResult {
            render_api: RenderApi::None,
            machine,
            imports: Vec::new(),
            needs_steam_api: false,
            needs_nvapi: false,
            needs_xinput: false,
            needs_xaudio: false,
        });
    }

    // Read section headers to resolve RVA → file offset
    let sections_offset = pe_offset as usize + 24 + optional_hdr_size;
    let sections_size = num_sections as usize * 40;
    let mut sections_buf = vec![0u8; sections_size];
    let n = unsafe {
        libc::pread(fd, sections_buf.as_mut_ptr() as *mut _, sections_size, sections_offset as i64)
    };
    if (n as usize) < sections_size {
        unsafe { libc::close(fd); }
        return None;
    }

    let rva_to_offset = |rva: u32| -> Option<u64> {
        for i in 0..num_sections as usize {
            let s = &sections_buf[i * 40..(i + 1) * 40];
            let virt_addr = u32::from_le_bytes([s[12], s[13], s[14], s[15]]);
            let virt_size = u32::from_le_bytes([s[8], s[9], s[10], s[11]]);
            let raw_offset = u32::from_le_bytes([s[20], s[21], s[22], s[23]]);
            if rva >= virt_addr && rva < virt_addr + virt_size {
                return Some((rva - virt_addr + raw_offset) as u64);
            }
        }
        None
    };

    // Read import directory entries (20 bytes each, null-terminated array)
    let mut imports = Vec::new();
    if let Some(import_file_offset) = rva_to_offset(import_rva) {
        let mut entry_buf = [0u8; 20];
        let mut idx = 0u64;
        loop {
            let n = unsafe {
                libc::pread(fd, entry_buf.as_mut_ptr() as *mut _, 20,
                    import_file_offset as i64 + (idx * 20) as i64)
            };
            if n != 20 { break; }

            // Check for null terminator (all zeros)
            if entry_buf.iter().all(|&b| b == 0) { break; }

            // Name RVA is at offset 12 in the import descriptor
            let name_rva = u32::from_le_bytes([
                entry_buf[12], entry_buf[13], entry_buf[14], entry_buf[15],
            ]);

            if let Some(name_offset) = rva_to_offset(name_rva) {
                let mut name_buf = [0u8; 128];
                let n = unsafe {
                    libc::pread(fd, name_buf.as_mut_ptr() as *mut _, 128, name_offset as i64)
                };
                if n > 0 {
                    let end = name_buf.iter().position(|&b| b == 0).unwrap_or(n as usize);
                    if let Ok(name) = std::str::from_utf8(&name_buf[..end]) {
                        imports.push(name.to_lowercase());
                    }
                }
            }

            idx += 1;
            if idx > 1000 { break; } // safety limit
        }
    }

    unsafe { libc::close(fd); }

    // Classify render API by import priority (most specific first)
    let render_api = if imports.iter().any(|s| s == "vulkan-1.dll") {
        RenderApi::Vulkan
    } else if imports.iter().any(|s| s == "d3d12.dll" || s == "d3d12core.dll") {
        RenderApi::DirectX12
    } else if imports.iter().any(|s| s == "d3d11.dll" || s == "d3d10.dll" || s == "d3d10core.dll" || s == "d3d10_1.dll") {
        RenderApi::DirectX11
    } else if imports.iter().any(|s| s == "d3d9.dll") {
        RenderApi::DirectX9
    } else if imports.iter().any(|s| s == "d3d8.dll" || s == "ddraw.dll") {
        RenderApi::DirectX8
    } else if imports.iter().any(|s| s == "opengl32.dll") {
        RenderApi::OpenGL
    } else {
        RenderApi::None
    };

    let needs_steam_api = imports.iter().any(|s| s == "steam_api64.dll" || s == "steam_api.dll");
    let needs_nvapi = imports.iter().any(|s| s == "nvapi64.dll" || s == "nvapi.dll");
    let needs_xinput = imports.iter().any(|s| s.starts_with("xinput"));
    let needs_xaudio = imports.iter().any(|s| s.starts_with("xaudio"));

    Some(PeScanResult {
        render_api,
        machine,
        imports,
        needs_steam_api,
        needs_nvapi,
        needs_xinput,
        needs_xaudio,
    })
}
