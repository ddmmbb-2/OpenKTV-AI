import { app, BrowserWindow, dialog, ipcMain, screen } from 'electron';
import { spawnSync } from 'child_process';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { startServer } from './server.js';
import { startPythonService, stopPythonService } from './python-service.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const serverPort = Number(process.env.KTV_NODE_PORT) || 3000;
app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required');
if (process.platform === 'win32' && process.stdout.isTTY) {
  spawnSync('cmd.exe', ['/d', '/s', '/c', 'chcp 65001 > nul'], {
    stdio: 'ignore',
    windowsHide: true,
  });
}
let mainWindow = null;
let playerWindow = null;
let pythonProcess = null;
let serverService = null;
let isQuitting = false;
let logDirectory = null;

function createWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.show();
    mainWindow.focus();
    return;
  }
  mainWindow = new BrowserWindow({
    width: 1500,
    height: 950,
    minWidth: 980,
    minHeight: 640,
    backgroundColor: '#10131b',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.cjs')
    },
    autoHideMenuBar: true,
    title: 'OpenKTV Master'
  });
  mainWindow.loadURL(`http://127.0.0.1:${serverPort}/operator`);

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function openPlayerWindow() {
  if (playerWindow && !playerWindow.isDestroyed()) {
    playerWindow.show();
    playerWindow.focus();
    return;
  }
  const displays = screen.getAllDisplays();
  const playerDisplay = displays.find(display => display.id !== screen.getPrimaryDisplay().id);
  playerWindow = new BrowserWindow({
    ...(playerDisplay ? { x: playerDisplay.bounds.x, y: playerDisplay.bounds.y } : {}),
    width: playerDisplay?.bounds.width || 1280,
    height: playerDisplay?.bounds.height || 720,
    backgroundColor: '#000000',
    autoHideMenuBar: true,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
    },
    title: 'OpenKTV Player',
  });
  playerWindow.loadURL(`http://127.0.0.1:${serverPort}/player`);
  if (playerDisplay) playerWindow.setFullScreen(true);
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('player:opened');
  }
  playerWindow.on('closed', () => {
    playerWindow = null;
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('player:closed');
    }
  });
}

function closePlayerWindow() {
  if (playerWindow && !playerWindow.isDestroyed()) {
    playerWindow.close();
    playerWindow = null;
  }
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('player:closed');
  }
}

ipcMain.handle('player:open', async () => {
  openPlayerWindow();
  return { ok: true, isFullscreen: Boolean(playerWindow?.isFullScreen()) };
});

ipcMain.handle('player:close', async () => {
  closePlayerWindow();
  return { ok: true };
});

ipcMain.handle('player:toggle-fullscreen', async () => {
  if (!playerWindow || playerWindow.isDestroyed()) {
    openPlayerWindow();
    return { ok: true, isFullscreen: true };
  }
  const next = !playerWindow.isFullScreen();
  playerWindow.setFullScreen(next);
  return { ok: true, isFullscreen: next };
});

ipcMain.handle('screen:get-displays', async () => {
  const primaryId = screen.getPrimaryDisplay().id;
  const displays = screen.getAllDisplays().map((d, index) => ({
    id: d.id,
    name: d.id === primaryId ? '內建主顯示器' : `外接顯示器 ${index}`,
    bounds: d.bounds,
    isPrimary: d.id === primaryId,
  }));
  const isOpen = Boolean(playerWindow && !playerWindow.isDestroyed());
  let playerDisplay = null;
  if (isOpen) {
    try {
      playerDisplay = screen.getDisplayMatching(playerWindow.getBounds());
    } catch {}
  }
  return {
    displays,
    playerOpen: isOpen,
    playerDisplayName: playerDisplay ? (playerDisplay.id === primaryId ? '內建顯示器' : '外接顯示器') : null,
    playerResolution: playerDisplay ? `${playerDisplay.bounds.width}×${playerDisplay.bounds.height}` : null,
    isFullscreen: isOpen ? Boolean(playerWindow.isFullScreen()) : false,
  };
});

app.whenReady().then(async () => {
  try {
    logDirectory = path.join(app.getPath('userData'), 'logs');
    fs.mkdirSync(logDirectory, { recursive: true });
    process.env.KTV_LOG_DIR = logDirectory;
    pythonProcess = await startPythonService();
    serverService = await startServer(serverPort);
    createWindow();
  } catch (error) {
    const detail = error.stack || error.message || String(error);
    const startupLog = logDirectory ? path.join(logDirectory, 'startup-error.log') : null;
    if (startupLog) {
      try {
        fs.writeFileSync(startupLog, `${new Date().toISOString()}\n${detail}\n`, 'utf8');
      } catch (logError) {
        console.error('[OpenKTV] Could not write startup log:', logError.message);
      }
    }
    console.error('[OpenKTV] Startup failed:', detail);
    dialog.showErrorBox(
      'OpenKTV 啟動失敗',
      `${error.message || '無法啟動服務。'}\n\n請確認 Python 相依套件已安裝，並檢查 Python 服務日誌。${startupLog ? `\n\n啟動錯誤：${startupLog}\nPython 日誌：${path.join(logDirectory, 'python-service.log')}` : ''}`,
    );
    app.quit();
  }

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('before-quit', event => {
  if (isQuitting) return;
  isQuitting = true;
  if (pythonProcess || serverService) {
    event.preventDefault();
    Promise.allSettled([
      serverService?.close(),
      pythonProcess ? stopPythonService(pythonProcess) : Promise.resolve(),
    ]).finally(() => {
      pythonProcess = null;
      serverService = null;
      app.quit();
    });
  }
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});