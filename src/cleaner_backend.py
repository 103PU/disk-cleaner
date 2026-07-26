import os
import sys
import json
import shutil
import subprocess
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingTCPServer
import urllib.parse

PORT = 8342

USER_PROFILE = os.environ.get('USERPROFILE', os.path.expanduser('~'))
PROGRAM_DATA = os.environ.get('PROGRAMDATA', r'C:\ProgramData')
SYSTEM_ROOT = os.environ.get('SystemRoot', r'C:\Windows')

# Expanded deep clean paths
TARGET_PATHS = {
    # 1. Developer Caches
    "uv_cache": {
        "name": "Python uv Cache",
        "path": os.path.join(USER_PROFILE, r"AppData\Local\uv\cache"),
        "description": "Cache for downloaded Python packages via uv tool.",
        "category": "dev"
    },
    "npm_cache": {
        "name": "Node.js npm Cache",
        "path": os.path.join(USER_PROFILE, r"AppData\Local\npm-cache"),
        "description": "Local cache of npm packages.",
        "category": "dev"
    },
    "pnpm_cache": {
        "name": "pnpm Store",
        "path": os.path.join(USER_PROFILE, r"AppData\Local\pnpm"),
        "description": "Global content-addressable store for pnpm packages.",
        "category": "dev"
    },
    "pip_cache": {
        "name": "pip Cache",
        "path": os.path.join(USER_PROFILE, r"AppData\Local\pip\cache"),
        "description": "Cache for downloaded Python packages via pip.",
        "category": "dev"
    },
    "nuget_cache": {
        "name": "NuGet Packages Cache",
        "path": os.path.join(USER_PROFILE, r".nuget\packages"),
        "description": "Cache of downloaded packages for .NET development.",
        "category": "dev"
    },
    
    # 2. Browser Caches
    "chrome_cache": {
        "name": "Chrome Browser Cache",
        "path": os.path.join(USER_PROFILE, r"AppData\Local\Google\Chrome\User Data\Default\Cache"),
        "description": "Google Chrome cached website files, images, and scripts.",
        "category": "browsers"
    },
    "chrome_code_cache": {
        "name": "Chrome Code Cache",
        "path": os.path.join(USER_PROFILE, r"AppData\Local\Google\Chrome\User Data\Default\Code Cache"),
        "description": "Google Chrome compiled V8 JavaScript engine caches.",
        "category": "browsers"
    },
    "edge_cache": {
        "name": "Edge Browser Cache",
        "path": os.path.join(USER_PROFILE, r"AppData\Local\Microsoft\Edge\User Data\Default\Cache"),
        "description": "Microsoft Edge cached website files.",
        "category": "browsers"
    },
    "edge_code_cache": {
        "name": "Edge Code Cache",
        "path": os.path.join(USER_PROFILE, r"AppData\Local\Microsoft\Edge\User Data\Default\Code Cache"),
        "description": "Microsoft Edge compiled script cache.",
        "category": "browsers"
    },

    # 3. IDE & System logs
    "vscode_cache": {
        "name": "VS Code General Cache",
        "path": os.path.join(USER_PROFILE, r"AppData\Roaming\Code\Cache"),
        "description": "Visual Studio Code editor local cache storage.",
        "category": "ide"
    },
    "vscode_cached_data": {
        "name": "VS Code Cached Data",
        "path": os.path.join(USER_PROFILE, r"AppData\Roaming\Code\CachedData"),
        "description": "VS Code cached workspace structure and auto-completions.",
        "category": "ide"
    },
    "vscode_vsixs": {
        "name": "VS Code Extension Installers",
        "path": os.path.join(USER_PROFILE, r"AppData\Roaming\Code\CachedExtensionVSIXs"),
        "description": "Temporary VS Code extensions installation packages (.vsix).",
        "category": "ide"
    },

    # 4. Windows Updates & System Delivery
    "windows_update": {
        "name": "Windows Update Download Cache",
        "path": os.path.join(SYSTEM_ROOT, r"SoftwareDistribution\Download"),
        "description": "Temporary downloaded Windows Update installation files.",
        "category": "system"
    },
    "delivery_optimization": {
        "name": "Delivery Optimization Cache",
        "path": os.path.join(SYSTEM_ROOT, r"ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache"),
        "description": "Windows Update peer-sharing download caches.",
        "category": "system"
    },
    
    # 5. Logs & Temporary Files
    "system_logs": {
        "name": "Windows System Logs",
        "path": os.path.join(SYSTEM_ROOT, r"Logs"),
        "description": "System events logging directories (including CBS update logs).",
        "category": "system"
    },
    "crash_dumps": {
        "name": "Application Crash Dumps",
        "path": os.path.join(USER_PROFILE, r"AppData\Local\CrashDumps"),
        "description": "Crash log dump files created by failing processes.",
        "category": "system"
    },
    "user_temp": {
        "name": "User Temp Folder",
        "path": os.path.join(USER_PROFILE, r"AppData\Local\Temp"),
        "description": "Temporary files created by running user applications.",
        "category": "temp"
    },
    "system_temp": {
        "name": "System Temp Folder",
        "path": os.path.join(SYSTEM_ROOT, r"Temp"),
        "description": "Temporary files created by Windows system services.",
        "category": "temp"
    }
}

DOCKER_FILES = {
    "docker_data_wsl": {
        "name": "Docker WSL2 Data VHDX",
        "path": os.path.join(USER_PROFILE, r"AppData\Local\Docker\wsl\disk\docker_data.vhdx"),
        "description": "Virtual hard disk storing Docker container data and images."
    },
    "docker_desktop_vm": {
        "name": "Docker Desktop VM VHDX",
        "path": os.path.join(PROGRAM_DATA, r"DockerDesktop\vm-data\DockerDesktop.vhdx"),
        "description": "Virtual hard disk for Docker Desktop application VM."
    }
}

CHROME_AI_DIR = os.path.join(USER_PROFILE, r"AppData\Local\Google\Chrome\User Data\OptGuideOnDeviceModel")
RECYCLE_BIN_PATH = r"C:\$Recycle.Bin"

class ThreadingHTTPServer(ThreadingTCPServer, HTTPServer):
    pass

def format_size(size_bytes):
    if size_bytes >= 1024**3:
        return f"{size_bytes / 1024**3:.2f} GB"
    elif size_bytes >= 1024**2:
        return f"{size_bytes / 1024**2:.2f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.2f} KB"
    else:
        return f"{size_bytes} B"

def get_folder_size(path):
    if not os.path.exists(path):
        return 0, 0
    total_size = 0
    file_count = 0
    try:
        # Check if directory path is system logs or locked path
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            for f in files:
                fp = os.path.join(root, f)
                if not os.path.islink(fp):
                    try:
                        total_size += os.path.getsize(fp)
                        file_count += 1
                    except Exception:
                        pass
    except Exception:
        pass
    return total_size, file_count

def check_chrome_ai_status():
    chrome_ai = {
        "name": "Google Chrome AI weights.bin",
        "path": CHROME_AI_DIR,
        "description": "On-device Gemini Nano weights downloaded by Google Chrome.",
        "size": 0,
        "is_locked": False,
        "lock_detected_as_dir": False
    }
    
    if not os.path.exists(CHROME_AI_DIR):
        return chrome_ai
        
    total_size = 0
    for root, dirs, files in os.walk(CHROME_AI_DIR):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total_size += os.path.getsize(fp)
            except Exception:
                pass
            
            # Check lock state
            if f == "weights.bin":
                if os.path.isdir(fp):
                    chrome_ai["is_locked"] = True
                    chrome_ai["lock_detected_as_dir"] = True
                    
    chrome_ai["size"] = total_size
    return chrome_ai

def get_vss_status():
    vss = {
        "name": "Volume Shadow Copies (VSS)",
        "allocated_bytes": 0,
        "max_bytes": 0,
        "description": "Windows System Restore points and shadow backups."
    }
    try:
        res = subprocess.run("vssadmin list shadowstorage", shell=True, text=True, capture_output=True, timeout=5)
        out = res.stdout
        
        # Parse allocated
        allocated_match = re.search(r"Allocated Shadow Copy Storage space:.*?([\d\.]+)\s*(KB|MB|GB|TB|B)", out, re.IGNORECASE)
        if allocated_match:
            val = float(allocated_match.group(1))
            unit = allocated_match.group(2).upper()
            mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
            vss["allocated_bytes"] = int(val * mult.get(unit, 1))
            
        # Parse max
        max_match = re.search(r"Maximum Shadow Copy Storage space:.*?([\d\.]+)\s*(KB|MB|GB|TB|B)", out, re.IGNORECASE)
        if max_match:
            val = float(max_match.group(1))
            unit = max_match.group(2).upper()
            mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
            vss["max_bytes"] = int(val * mult.get(unit, 1))
    except Exception:
        pass
    return vss

def get_c_drive_space():
    try:
        total, used, free = shutil.disk_usage("C:\\")
        return {
            "total": total,
            "used": used,
            "free": free,
            "free_percent": (free / total) * 100
        }
    except Exception:
        return {"total": 0, "used": 0, "free": 0, "free_percent": 0}

def force_delete_file(filepath):
    try:
        os.chmod(filepath, 0o777)
        os.remove(filepath)
        return True
    except Exception:
        return False

def clean_target_folder(path):
    logs = []
    if not os.path.exists(path):
        logs.append(f"Directory {path} does not exist. Skipping.")
        return logs
        
    logs.append(f"Scanning files to delete in {path}...")
    deleted_files = 0
    deleted_dirs = 0
    locked_files = 0
    
    for root, dirs, files in os.walk(path, topdown=False):
        # Skip directories that are junction points or symlinks
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        
        for name in files:
            filepath = os.path.join(root, name)
            if force_delete_file(filepath):
                deleted_files += 1
            else:
                locked_files += 1
                
        for name in dirs:
            dirpath = os.path.join(root, name)
            try:
                os.rmdir(dirpath)
                deleted_dirs += 1
            except Exception:
                pass
                
    logs.append(f"Cleanup finished. Deleted {deleted_files} files, {deleted_dirs} folders. {locked_files} files were locked (in-use).")
    return logs

class DiskCleanerAPIHandler(BaseHTTPRequestHandler):
    def send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_cors_headers()
            self.end_headers()
            try:
                script_dir = os.path.dirname(os.path.abspath(__file__))
                ui_path = os.path.join(script_dir, "cleaner_ui.html")
                with open(ui_path, "r", encoding="utf-8") as f:
                    self.wfile.write(f.read().encode("utf-8"))
            except Exception as e:
                self.wfile.write(f"Error loading UI: {e}".encode("utf-8"))
            return

        elif path == "/api/status":
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_cors_headers()
            self.end_headers()
            
            space = get_c_drive_space()
            self.wfile.write(json.dumps(space).encode('utf-8'))
            return

        elif path == "/api/scan":
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_cors_headers()
            self.end_headers()
            
            results = {
                "c_drive": get_c_drive_space(),
                "categories": {},
                "docker": {},
                "chrome_ai": check_chrome_ai_status(),
                "vss": get_vss_status(),
                "recycle_bin": {"size_bytes": 0, "size_str": "0 B"}
            }
            
            # Scan general target paths
            for key, info in TARGET_PATHS.items():
                size, count = get_folder_size(info["path"])
                results["categories"][key] = {
                    "name": info["name"],
                    "path": info["path"],
                    "description": info["description"],
                    "size_bytes": size,
                    "size_str": format_size(size),
                    "file_count": count
                }
                
            # Scan Docker VHDX files
            for key, info in DOCKER_FILES.items():
                size = 0
                exists = os.path.exists(info["path"])
                if exists:
                    try:
                        size = os.path.getsize(info["path"])
                    except Exception:
                        pass
                results["docker"][key] = {
                    "name": info["name"],
                    "path": info["path"],
                    "description": info["description"],
                    "size_bytes": size,
                    "size_str": format_size(size) if exists else "Not Installed/Not Found",
                    "exists": exists
                }
                
            # Recycle bin scan
            bin_size, bin_count = get_folder_size(RECYCLE_BIN_PATH)
            results["recycle_bin"] = {
                "size_bytes": bin_size,
                "size_str": format_size(bin_size),
                "path": RECYCLE_BIN_PATH,
                "file_count": bin_count
            }
                
            self.wfile.write(json.dumps(results).encode('utf-8'))
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if path == "/api/clean":
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            params = json.loads(post_data.decode('utf-8'))
            
            selected = params.get("categories", [])
            options = params.get("options", {})
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_cors_headers()
            self.end_headers()
            
            logs = []
            logs.append("=== STARTING CLEANUP PROCESS ===")
            
            # 1. Clean general directories
            for cat_key in selected:
                if cat_key in TARGET_PATHS:
                    info = TARGET_PATHS[cat_key]
                    logs.append(f"\n[Cleaning {info['name']}]")
                    logs.extend(clean_target_folder(info["path"]))
                    
            # 2. Chrome AI delete
            if "chrome_ai" in selected:
                logs.append("\n[Cleaning Google Chrome AI weights]")
                target_weights = None
                if os.path.exists(CHROME_AI_DIR):
                    for root, dirs, files in os.walk(CHROME_AI_DIR):
                        for f in files:
                            if f == "weights.bin":
                                target_weights = os.path.join(root, f)
                                break
                                
                if target_weights:
                    if os.path.isdir(target_weights):
                        logs.append("Chrome weights.bin path is currently blocked (folder lock). Skipping deletion.")
                    else:
                        logs.append(f"Deleting Chrome AI weights file at {target_weights}...")
                        if force_delete_file(target_weights):
                            logs.append("Successfully deleted weights.bin file.")
                        else:
                            logs.append("Failed to delete weights.bin (Access Denied / File locked by Google Chrome).")
                else:
                    logs.append("No active weights.bin file found in Google Chrome directories.")
                    
            # 3. Docker compact
            if "docker_compact" in selected:
                logs.append("\n[Running Docker WSL2 VHDX Compaction]")
                logs.append("Shutting down WSL instances...")
                res_wsl = subprocess.run("wsl --shutdown", shell=True, text=True, capture_output=True, timeout=15)
                logs.append(f"WSL Shutdown output: {res_wsl.stdout.strip()} {res_wsl.stderr.strip()}")
                
                for key, info in DOCKER_FILES.items():
                    vhdx_path = info["path"]
                    if os.path.exists(vhdx_path):
                        logs.append(f"Compacting {info['name']}...")
                        script_path = os.path.join(os.path.dirname(vhdx_path), "diskpart_compact.txt")
                        try:
                            with open(script_path, "w") as dp:
                                dp.write(f'select vdisk file="{vhdx_path}"\n')
                                dp.write('attach vdisk readonly\n')
                                dp.write('compact vdisk\n')
                                dp.write('detach vdisk\n')
                            
                            res_dp = subprocess.run(f"diskpart /s \"{script_path}\"", shell=True, text=True, capture_output=True, timeout=60)
                            logs.append("Diskpart executed successfully.")
                            logs.append(res_dp.stdout)
                            if res_dp.stderr:
                                logs.append(f"Diskpart error output: {res_dp.stderr}")
                            
                            if os.path.exists(script_path):
                                os.remove(script_path)
                        except Exception as e:
                            logs.append(f"Failed to compact {info['name']}: {e}")
                    else:
                        logs.append(f"{info['name']} not found at {vhdx_path}. Skipping compaction.")
                        
            # 4. VSS cleaning and limit
            if "vss_cleanup" in selected:
                logs.append("\n[Cleaning Volume Shadow Copies]")
                res_vss = subprocess.run("vssadmin delete shadows /all /quiet", shell=True, text=True, capture_output=True, timeout=15)
                logs.append("VSS shadow copies deleted.")
                if res_vss.stdout: logs.append(res_vss.stdout.strip())
                
                if options.get("vss_limit_2gb", False):
                    logs.append("Setting maximum shadow copy storage space to 2GB...")
                    res_limit = subprocess.run("vssadmin resize shadowstorage /for=c: /on=c: /maxsize=2GB", shell=True, text=True, capture_output=True, timeout=15)
                    logs.append("Shadow copy storage resized.")
                    if res_limit.stdout: logs.append(res_limit.stdout.strip())
                    if res_limit.stderr: logs.append(res_limit.stderr.strip())
                    
            # 5. Recycle bin purge
            if "recycle_bin" in selected:
                logs.append("\n[Emptying Recycle Bin]")
                # PowerShell Recycle Bin Clean
                res_bin = subprocess.run("powershell -Command \"Clear-RecycleBin -Force -ErrorAction SilentlyContinue\"", shell=True, text=True, capture_output=True, timeout=15)
                # Python fallback deep folder clean on C:\$Recycle.Bin
                logs.extend(clean_target_folder(RECYCLE_BIN_PATH))
                logs.append("Recycle Bin emptied successfully.")
                
            logs.append("\n=== CLEANUP COMPLETED ===")
            
            space_after = get_c_drive_space()
            self.wfile.write(json.dumps({
                "logs": logs,
                "space_after": space_after
            }).encode('utf-8'))
            return

        elif path == "/api/chrome-lock":
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            params = json.loads(post_data.decode('utf-8'))
            
            action = params.get("action", "check")
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_cors_headers()
            self.end_headers()
            
            logs = []
            
            target_weights_path = None
            if os.path.exists(CHROME_AI_DIR):
                for entry in os.scandir(CHROME_AI_DIR):
                    if entry.is_dir():
                        test_path = os.path.join(entry.path, "weights.bin")
                        target_weights_path = test_path
                        break
                        
            if not target_weights_path:
                target_weights_path = os.path.join(CHROME_AI_DIR, "default", "weights.bin")
                
            parent_dir = os.path.dirname(target_weights_path)
            
            if action == "lock":
                logs.append("Initiating Chrome AI Lock mechanism...")
                try:
                    os.makedirs(parent_dir, exist_ok=True)
                    
                    if os.path.exists(target_weights_path):
                        if os.path.isfile(target_weights_path):
                            os.chmod(target_weights_path, 0o777)
                            os.remove(target_weights_path)
                            logs.append("Deleted existing weights.bin file.")
                        elif os.path.isdir(target_weights_path):
                            logs.append("Lock directory already exists at weights.bin path.")
                            
                    os.makedirs(target_weights_path, exist_ok=True)
                    logs.append("Created block directory 'weights.bin'.")
                    
                    subprocess.run(f'attrib +r +h +s "{target_weights_path}"', shell=True)
                    logs.append("Applied Read-Only, Hidden, and System system flags.")
                except Exception as e:
                    logs.append(f"Failed to lock Chrome AI weights: {e}")
                    
            elif action == "unlock":
                logs.append("Initiating Chrome AI Unlock mechanism...")
                try:
                    if os.path.exists(target_weights_path):
                        if os.path.isdir(target_weights_path):
                            subprocess.run(f'attrib -r -h -s "{target_weights_path}"', shell=True)
                            os.rmdir(target_weights_path)
                            logs.append("Removed block directory 'weights.bin'. Chrome can now download weights.")
                        elif os.path.isfile(target_weights_path):
                            logs.append("Chrome AI weights is already in a file format. No folder lock detected.")
                    else:
                        logs.append("No block directory or weights file found. Already unlocked.")
                except Exception as e:
                    logs.append(f"Failed to unlock Chrome AI weights: {e}")
                    
            status = check_chrome_ai_status()
            self.wfile.write(json.dumps({
                "logs": logs,
                "status": status
            }).encode('utf-8'))
            return

        elif path == "/api/open-folder":
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            params = json.loads(post_data.decode('utf-8'))
            folder_path = params.get("path")
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_cors_headers()
            self.end_headers()
            
            success = False
            if folder_path and os.path.exists(folder_path):
                try:
                    # open folder in windows file explorer
                    os.startfile(folder_path)
                    success = True
                except Exception:
                    pass
            self.wfile.write(json.dumps({"success": success}).encode('utf-8'))
            return

        self.send_response(404)
        self.end_headers()

def run_server():
    server_address = ('127.0.0.1', PORT)
    httpd = ThreadingHTTPServer(server_address, DiskCleanerAPIHandler)
    print(f"Starting Antigravity Disk Cleaner API Server on 127.0.0.1:{PORT}...")
    try:
        print(f"Server is running. Open http://localhost:{PORT}/ in your browser.")
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("Server is stopping...")
        httpd.server_close()
        print("Server stopped.")

if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    run_server()
