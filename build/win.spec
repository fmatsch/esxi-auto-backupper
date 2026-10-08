# PyInstaller-Spec für den Windows-Build (auf einem Windows-Rechner ausführen):
#   .venv\Scripts\pyinstaller build\win.spec
# Ergebnis in dist\:
#   EsxiBackupper.exe     GUI-App (ohne Konsolenfenster)
#   EsxiBackupperCli.exe  Konsolenversion für Taskplaner/--run-job (mit Log-Ausgabe)

from PyInstaller.utils.hooks import collect_submodules

hidden = collect_submodules("pyVmomi") + collect_submodules("keyring.backends")
ICON = "../assets/icon.ico"

a = Analysis(
    ["../run_esxi_backupper.py"],
    pathex=[".."],
    datas=[("../esxi_backupper/assets", "esxi_backupper/assets")],
    hiddenimports=hidden,
    excludes=["tkinter"],
)

pyz = PYZ(a.pure)

exe_gui = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="EsxiBackupper",
    icon=ICON,
    console=False,
    upx=False,
)

exe_cli = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="EsxiBackupperCli",
    icon=ICON,
    console=True,
    upx=False,
)
