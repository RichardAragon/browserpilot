from pathlib import Path
import os
import shutil
import sys

def codex_path():
    found=shutil.which('codex')
    if found:return found
    root=Path(os.environ.get('LOCALAPPDATA',''))/'OpenAI'/'Codex'/'bin'
    candidates=sorted(root.glob('*/codex.exe'),key=lambda p:p.stat().st_mtime,reverse=True)
    return str(candidates[0]) if candidates else None

def resource_root():return Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[1]))

def configure_browser():
    bundled=resource_root()/'browsers'
    if bundled.exists():os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(bundled)

def health():
    from playwright.sync_api import sync_playwright
    configure_browser()
    browser_root=Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH',Path(os.environ.get('LOCALAPPDATA',''))/'ms-playwright'))
    browser=any(browser_root.glob('chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe'))
    edge=Path(os.environ.get('PROGRAMFILES(X86)','C:/Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe'
    return {'browser':browser,'social_browser':edge.exists(),'planner':bool(codex_path()),'planner_label':'Codex CLI · existing sign-in required','version':'1.1.0-preview','data_local':True}
