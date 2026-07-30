#!/usr/bin/env python3
"""
Fox Client Windows Installer
============================
This script creates a self-installing executable for the Fox client.
When run, it:
1. Shows a nice GUI installer
2. Copies fox.exe to the installation directory
3. Adds the directory to PATH
4. Creates Start Menu shortcuts
5. Registers in Windows Programs list for easy uninstall

Build this with: pyinstaller --onefile --windowed --name=FoxNest-Setup fox_installer.py
"""

import os
import sys
import shutil
import ctypes
import subprocess
import winreg
import tempfile
import base64
import zlib
from pathlib import Path

# ============================================================================
# EMBEDDED FOX.EXE DATA
# This will be populated during the build process
# ============================================================================
EMBEDDED_FOX_EXE = None  # Will be set by build script or loaded from file

# ============================================================================
# INSTALLER CONFIGURATION
# ============================================================================
APP_NAME = "FoxNest"
APP_VERSION = "1.0.0"
APP_PUBLISHER = "FoxNest Team"
APP_DESCRIPTION = "FoxNest Version Control System"
DEFAULT_INSTALL_DIR = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'FoxNest')

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def is_admin():
    """Check if running with administrator privileges"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def run_as_admin():
    """Re-run this script with admin privileges"""
    if sys.platform == 'win32':
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, " ".join(sys.argv), None, 1
        )

def get_fox_exe_data():
    """Get the fox.exe binary data"""
    global EMBEDDED_FOX_EXE
    
    # First check if we have embedded data
    if EMBEDDED_FOX_EXE:
        return zlib.decompress(base64.b64decode(EMBEDDED_FOX_EXE))
    
    # Otherwise, look for fox.exe in various locations
    search_paths = [
        Path(sys.executable).parent / "fox.exe",
        Path(__file__).parent / "fox.exe",
        Path(__file__).parent.parent / "dist" / "fox.exe",
        Path(__file__).parent / "dist" / "fox.exe",
        Path.cwd() / "fox.exe",
        Path.cwd() / "dist" / "fox.exe",
    ]
    
    # If running as frozen exe, check temp extraction dir
    if getattr(sys, 'frozen', False):
        search_paths.insert(0, Path(sys._MEIPASS) / "fox.exe")
    
    for path in search_paths:
        if path.exists():
            with open(path, 'rb') as f:
                return f.read()
    
    return None

def broadcast_environment_change():
    """Notify Windows that environment variables have changed"""
    try:
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x1A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_long()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            "Environment",
            SMTO_ABORTIFHUNG,
            5000,
            ctypes.byref(result)
        )
    except:
        pass

def add_to_path(directory, system_wide=False):
    """Add directory to PATH environment variable"""
    try:
        if system_wide:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
                0, winreg.KEY_ALL_ACCESS
            )
        else:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Environment",
                0, winreg.KEY_ALL_ACCESS
            )
        
        try:
            current_path, _ = winreg.QueryValueEx(key, "Path")
        except WindowsError:
            current_path = ""
        
        # Check if already in PATH
        paths = [p.strip().lower() for p in current_path.split(';') if p.strip()]
        if directory.lower() in paths:
            winreg.CloseKey(key)
            return True
        
        # Add to PATH
        new_path = current_path.rstrip(';') + ';' + directory
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
        winreg.CloseKey(key)
        
        broadcast_environment_change()
        return True
        
    except Exception as e:
        print(f"Error adding to PATH: {e}")
        return False

def remove_from_path(directory, system_wide=False):
    """Remove directory from PATH environment variable"""
    try:
        if system_wide:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
                0, winreg.KEY_ALL_ACCESS
            )
        else:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Environment",
                0, winreg.KEY_ALL_ACCESS
            )
        
        try:
            current_path, _ = winreg.QueryValueEx(key, "Path")
        except WindowsError:
            winreg.CloseKey(key)
            return True
        
        # Remove from PATH
        paths = [p.strip() for p in current_path.split(';') if p.strip()]
        new_paths = [p for p in paths if p.lower() != directory.lower()]
        new_path = ';'.join(new_paths)
        
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
        winreg.CloseKey(key)
        
        broadcast_environment_change()
        return True
        
    except Exception as e:
        print(f"Error removing from PATH: {e}")
        return False

def create_uninstaller(install_dir):
    """Create uninstaller registry entries"""
    try:
        uninstall_key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}"
        )
        
        uninstaller_path = os.path.join(install_dir, "uninstall.exe")
        
        winreg.SetValueEx(uninstall_key, "DisplayName", 0, winreg.REG_SZ, f"{APP_NAME} - {APP_DESCRIPTION}")
        winreg.SetValueEx(uninstall_key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
        winreg.SetValueEx(uninstall_key, "Publisher", 0, winreg.REG_SZ, APP_PUBLISHER)
        winreg.SetValueEx(uninstall_key, "UninstallString", 0, winreg.REG_SZ, f'"{uninstaller_path}"')
        winreg.SetValueEx(uninstall_key, "InstallLocation", 0, winreg.REG_SZ, install_dir)
        winreg.SetValueEx(uninstall_key, "DisplayIcon", 0, winreg.REG_SZ, os.path.join(install_dir, "fox.exe"))
        winreg.SetValueEx(uninstall_key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(uninstall_key, "NoRepair", 0, winreg.REG_DWORD, 1)
        
        winreg.CloseKey(uninstall_key)
        return True
    except Exception as e:
        print(f"Error creating uninstaller registry: {e}")
        return False

def remove_uninstaller_registry():
    """Remove uninstaller registry entries"""
    try:
        winreg.DeleteKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}"
        )
        return True
    except:
        return False

def create_start_menu_shortcut(install_dir):
    """Create Start Menu shortcut"""
    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        
        # Get Start Menu Programs folder
        start_menu = shell.SpecialFolders("Programs")
        foxnest_folder = os.path.join(start_menu, APP_NAME)
        os.makedirs(foxnest_folder, exist_ok=True)
        
        # Create shortcut
        shortcut_path = os.path.join(foxnest_folder, f"{APP_NAME} Command Line.lnk")
        shortcut = shell.CreateShortCut(shortcut_path)
        shortcut.Targetpath = "cmd.exe"
        shortcut.Arguments = f'/k "echo {APP_NAME} Version Control System && echo. && echo Type: fox --help && echo."'
        shortcut.WorkingDirectory = os.path.expanduser("~")
        shortcut.IconLocation = os.path.join(install_dir, "fox.exe")
        shortcut.save()
        
        return True
    except ImportError:
        # pywin32 not available, skip shortcut creation
        return False
    except Exception as e:
        print(f"Error creating shortcut: {e}")
        return False

def remove_start_menu_shortcut():
    """Remove Start Menu shortcut"""
    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        start_menu = shell.SpecialFolders("Programs")
        foxnest_folder = os.path.join(start_menu, APP_NAME)
        
        if os.path.exists(foxnest_folder):
            shutil.rmtree(foxnest_folder)
        return True
    except:
        return False

# ============================================================================
# GUI INSTALLER (using tkinter)
# ============================================================================

def run_gui_installer():
    """Run the graphical installer"""
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    
    class InstallerApp:
        def __init__(self, root):
            self.root = root
            self.root.title(f"{APP_NAME} Setup")
            self.root.geometry("550x450")
            self.root.resizable(False, False)
            
            # Center window
            self.root.update_idletasks()
            x = (self.root.winfo_screenwidth() - 550) // 2
            y = (self.root.winfo_screenheight() - 450) // 2
            self.root.geometry(f"+{x}+{y}")
            
            # Variables
            self.install_dir = tk.StringVar(value=DEFAULT_INSTALL_DIR)
            self.add_to_path_var = tk.BooleanVar(value=True)
            self.create_shortcut_var = tk.BooleanVar(value=True)
            
            # Style
            style = ttk.Style()
            style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
            style.configure("Subtitle.TLabel", font=("Segoe UI", 10))
            style.configure("Section.TLabel", font=("Segoe UI", 10, "bold"))
            
            self.create_welcome_page()
        
        def clear_page(self):
            for widget in self.root.winfo_children():
                widget.destroy()
        
        def create_welcome_page(self):
            self.clear_page()
            
            # Header
            header_frame = ttk.Frame(self.root, padding=20)
            header_frame.pack(fill="x")
            
            ttk.Label(
                header_frame, 
                text=f"🦊 Welcome to {APP_NAME} Setup",
                style="Title.TLabel"
            ).pack(anchor="w")
            
            ttk.Label(
                header_frame,
                text=f"Version {APP_VERSION}",
                style="Subtitle.TLabel"
            ).pack(anchor="w", pady=(5, 0))
            
            # Separator
            ttk.Separator(self.root, orient="horizontal").pack(fill="x", padx=20)
            
            # Content
            content_frame = ttk.Frame(self.root, padding=20)
            content_frame.pack(fill="both", expand=True)
            
            ttk.Label(
                content_frame,
                text="This wizard will install FoxNest on your computer.\n\n"
                     "FoxNest is a lightweight version control system designed\n"
                     "for easy collaboration and code management.\n\n"
                     "Features:\n"
                     "• Git-like commands (fox add, commit, push, pull)\n"
                     "• Compressed storage with delta encoding\n"
                     "• Simple setup and configuration\n"
                     "• Works on any Windows PC",
                justify="left"
            ).pack(anchor="w")
            
            # Buttons
            button_frame = ttk.Frame(self.root, padding=20)
            button_frame.pack(fill="x", side="bottom")
            
            ttk.Button(
                button_frame,
                text="Cancel",
                command=self.root.quit
            ).pack(side="left")
            
            ttk.Button(
                button_frame,
                text="Next →",
                command=self.create_options_page
            ).pack(side="right")
        
        def create_options_page(self):
            self.clear_page()
            
            # Header
            header_frame = ttk.Frame(self.root, padding=20)
            header_frame.pack(fill="x")
            
            ttk.Label(
                header_frame,
                text="Installation Options",
                style="Title.TLabel"
            ).pack(anchor="w")
            
            # Separator
            ttk.Separator(self.root, orient="horizontal").pack(fill="x", padx=20)
            
            # Content
            content_frame = ttk.Frame(self.root, padding=20)
            content_frame.pack(fill="both", expand=True)
            
            # Install directory
            ttk.Label(
                content_frame,
                text="Installation Directory:",
                style="Section.TLabel"
            ).pack(anchor="w", pady=(0, 5))
            
            dir_frame = ttk.Frame(content_frame)
            dir_frame.pack(fill="x", pady=(0, 15))
            
            ttk.Entry(
                dir_frame,
                textvariable=self.install_dir,
                width=50
            ).pack(side="left", fill="x", expand=True)
            
            ttk.Button(
                dir_frame,
                text="Browse...",
                command=self.browse_directory
            ).pack(side="right", padx=(10, 0))
            
            # Options
            ttk.Label(
                content_frame,
                text="Options:",
                style="Section.TLabel"
            ).pack(anchor="w", pady=(10, 5))
            
            ttk.Checkbutton(
                content_frame,
                text="Add to system PATH (recommended)",
                variable=self.add_to_path_var
            ).pack(anchor="w", pady=2)
            
            ttk.Checkbutton(
                content_frame,
                text="Create Start Menu shortcut",
                variable=self.create_shortcut_var
            ).pack(anchor="w", pady=2)
            
            # Info
            info_frame = ttk.Frame(content_frame)
            info_frame.pack(fill="x", pady=(20, 0))
            
            ttk.Label(
                info_frame,
                text="ℹ️ Adding to PATH allows you to run 'fox' from any directory.",
                foreground="gray"
            ).pack(anchor="w")
            
            # Buttons
            button_frame = ttk.Frame(self.root, padding=20)
            button_frame.pack(fill="x", side="bottom")
            
            ttk.Button(
                button_frame,
                text="← Back",
                command=self.create_welcome_page
            ).pack(side="left")
            
            ttk.Button(
                button_frame,
                text="Cancel",
                command=self.root.quit
            ).pack(side="left", padx=10)
            
            ttk.Button(
                button_frame,
                text="Install",
                command=self.start_installation
            ).pack(side="right")
        
        def browse_directory(self):
            directory = filedialog.askdirectory(
                initialdir=self.install_dir.get(),
                title="Select Installation Directory"
            )
            if directory:
                self.install_dir.set(directory)
        
        def start_installation(self):
            self.clear_page()
            
            # Header
            header_frame = ttk.Frame(self.root, padding=20)
            header_frame.pack(fill="x")
            
            ttk.Label(
                header_frame,
                text="Installing...",
                style="Title.TLabel"
            ).pack(anchor="w")
            
            # Separator
            ttk.Separator(self.root, orient="horizontal").pack(fill="x", padx=20)
            
            # Content
            content_frame = ttk.Frame(self.root, padding=20)
            content_frame.pack(fill="both", expand=True)
            
            # Progress
            self.progress_var = tk.DoubleVar()
            self.progress = ttk.Progressbar(
                content_frame,
                variable=self.progress_var,
                maximum=100,
                length=400
            )
            self.progress.pack(pady=20)
            
            self.status_label = ttk.Label(content_frame, text="Preparing installation...")
            self.status_label.pack()
            
            self.log_text = tk.Text(content_frame, height=10, width=60, state="disabled")
            self.log_text.pack(pady=20, fill="both", expand=True)
            
            # Start installation in background
            self.root.after(100, self.do_installation)
        
        def log(self, message):
            self.log_text.config(state="normal")
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
            self.root.update()
        
        def do_installation(self):
            install_dir = self.install_dir.get()
            success = True
            
            try:
                # Step 1: Create directory
                self.status_label.config(text="Creating installation directory...")
                self.progress_var.set(10)
                self.log(f"Creating directory: {install_dir}")
                os.makedirs(install_dir, exist_ok=True)
                self.log("✓ Directory created")
                
                # Step 2: Extract fox.exe
                self.status_label.config(text="Extracting fox.exe...")
                self.progress_var.set(30)
                self.log("Extracting fox.exe...")
                
                fox_data = get_fox_exe_data()
                if not fox_data:
                    raise Exception("Could not find fox.exe data. Make sure fox.exe is in the same directory as the installer.")
                
                fox_path = os.path.join(install_dir, "fox.exe")
                with open(fox_path, 'wb') as f:
                    f.write(fox_data)
                self.log(f"✓ Extracted fox.exe ({len(fox_data) / 1024 / 1024:.2f} MB)")
                
                # Step 3: Copy uninstaller
                self.status_label.config(text="Creating uninstaller...")
                self.progress_var.set(50)
                self.log("Creating uninstaller...")
                
                # Copy this installer as uninstaller
                if getattr(sys, 'frozen', False):
                    installer_path = sys.executable
                    uninstaller_path = os.path.join(install_dir, "uninstall.exe")
                    shutil.copy2(installer_path, uninstaller_path)
                    self.log("✓ Uninstaller created")
                else:
                    self.log("⚠ Running from source, skipping uninstaller copy")
                
                # Step 4: Add to PATH
                if self.add_to_path_var.get():
                    self.status_label.config(text="Adding to PATH...")
                    self.progress_var.set(70)
                    self.log("Adding to PATH...")
                    
                    if add_to_path(install_dir):
                        self.log("✓ Added to PATH")
                    else:
                        self.log("⚠ Could not add to PATH automatically")
                
                # Step 5: Create shortcuts
                if self.create_shortcut_var.get():
                    self.status_label.config(text="Creating shortcuts...")
                    self.progress_var.set(80)
                    self.log("Creating Start Menu shortcut...")
                    
                    if create_start_menu_shortcut(install_dir):
                        self.log("✓ Start Menu shortcut created")
                    else:
                        self.log("⚠ Could not create shortcut (pywin32 not available)")
                
                # Step 6: Register uninstaller
                self.status_label.config(text="Registering application...")
                self.progress_var.set(90)
                self.log("Registering with Windows...")
                
                if create_uninstaller(install_dir):
                    self.log("✓ Registered in Programs and Features")
                
                # Done
                self.progress_var.set(100)
                self.status_label.config(text="Installation complete!")
                self.log("\n✓ Installation completed successfully!")
                
            except Exception as e:
                success = False
                self.log(f"\n✗ Error: {str(e)}")
                self.status_label.config(text="Installation failed!")
            
            # Show completion page
            self.root.after(1000, lambda: self.create_completion_page(success, install_dir))
        
        def create_completion_page(self, success, install_dir):
            self.clear_page()
            
            # Header
            header_frame = ttk.Frame(self.root, padding=20)
            header_frame.pack(fill="x")
            
            if success:
                ttk.Label(
                    header_frame,
                    text="✓ Installation Complete!",
                    style="Title.TLabel"
                ).pack(anchor="w")
            else:
                ttk.Label(
                    header_frame,
                    text="✗ Installation Failed",
                    style="Title.TLabel"
                ).pack(anchor="w")
            
            # Separator
            ttk.Separator(self.root, orient="horizontal").pack(fill="x", padx=20)
            
            # Content
            content_frame = ttk.Frame(self.root, padding=20)
            content_frame.pack(fill="both", expand=True)
            
            if success:
                ttk.Label(
                    content_frame,
                    text=f"{APP_NAME} has been installed successfully!\n\n"
                         f"Installation directory:\n{install_dir}\n\n"
                         "To get started:\n"
                         "1. Open a NEW Command Prompt or PowerShell\n"
                         "2. Type: fox --help\n"
                         "3. Initialize a repository: fox init\n\n"
                         "⚠️ Important: You must open a NEW terminal window\n"
                         "for the PATH changes to take effect!",
                    justify="left"
                ).pack(anchor="w")
            else:
                ttk.Label(
                    content_frame,
                    text="The installation could not be completed.\n\n"
                         "Please try the following:\n"
                         "• Run the installer as Administrator\n"
                         "• Check that you have write access to the install directory\n"
                         "• Make sure fox.exe is in the same folder as the installer",
                    justify="left"
                ).pack(anchor="w")
            
            # Buttons
            button_frame = ttk.Frame(self.root, padding=20)
            button_frame.pack(fill="x", side="bottom")
            
            ttk.Button(
                button_frame,
                text="Finish",
                command=self.root.quit
            ).pack(side="right")
    
    root = tk.Tk()
    app = InstallerApp(root)
    root.mainloop()

# ============================================================================
# UNINSTALLER
# ============================================================================

def run_uninstaller():
    """Run the uninstaller"""
    import tkinter as tk
    from tkinter import ttk, messagebox
    
    # Find install directory from registry
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}",
            0, winreg.KEY_READ
        )
        install_dir, _ = winreg.QueryValueEx(key, "InstallLocation")
        winreg.CloseKey(key)
    except:
        install_dir = DEFAULT_INSTALL_DIR
    
    root = tk.Tk()
    root.withdraw()
    
    result = messagebox.askyesno(
        f"Uninstall {APP_NAME}",
        f"Are you sure you want to uninstall {APP_NAME}?\n\n"
        f"Installation directory:\n{install_dir}"
    )
    
    if not result:
        root.destroy()
        return
    
    # Perform uninstallation
    errors = []
    
    # Remove from PATH
    try:
        remove_from_path(install_dir)
    except Exception as e:
        errors.append(f"Could not remove from PATH: {e}")
    
    # Remove Start Menu shortcut
    try:
        remove_start_menu_shortcut()
    except Exception as e:
        errors.append(f"Could not remove shortcut: {e}")
    
    # Remove registry entry
    try:
        remove_uninstaller_registry()
    except Exception as e:
        errors.append(f"Could not remove registry entry: {e}")
    
    # Remove files (except uninstaller itself)
    try:
        for item in os.listdir(install_dir):
            item_path = os.path.join(install_dir, item)
            if item != "uninstall.exe":
                if os.path.isfile(item_path):
                    os.remove(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
    except Exception as e:
        errors.append(f"Could not remove files: {e}")
    
    # Create batch file to remove remaining files and itself
    cleanup_bat = os.path.join(tempfile.gettempdir(), "foxnest_cleanup.bat")
    try:
        with open(cleanup_bat, 'w') as f:
            f.write('@echo off\n')
            f.write('timeout /t 2 /nobreak >nul\n')
            f.write(f'rmdir /s /q "{install_dir}" 2>nul\n')
            f.write(f'del /f /q "{cleanup_bat}"\n')
        
        subprocess.Popen(
            ['cmd', '/c', cleanup_bat],
            creationflags=subprocess.CREATE_NO_WINDOW
        )
    except:
        pass
    
    if errors:
        messagebox.showwarning(
            "Uninstall Complete",
            f"{APP_NAME} has been uninstalled with some warnings:\n\n" +
            "\n".join(errors) +
            "\n\nYou may need to manually remove some components."
        )
    else:
        messagebox.showinfo(
            "Uninstall Complete",
            f"{APP_NAME} has been successfully uninstalled."
        )
    
    root.destroy()

# ============================================================================
# MAIN
# ============================================================================

def main():
    # Check if this is the uninstaller
    exe_name = os.path.basename(sys.executable).lower()
    if 'uninstall' in exe_name or '--uninstall' in sys.argv:
        run_uninstaller()
    else:
        run_gui_installer()

if __name__ == "__main__":
    main()

