import { app, BrowserWindow, Menu, shell } from 'electron';
import { pathToFileURL } from 'url';
import { isDevMode } from './runtime-paths';
import { sameDocumentLocation } from '../shared/navigation-policy';
import * as path from 'path';
import { setupIpcHandlers } from './ipc-handlers';
import { pythonBridge } from './python-bridge';

// Chromium sessions/localStorage are separate as well as broker persistence.
if (isDevMode()) app.setPath('userData', path.join(app.getPath('userData'), 'dev'));
const trustedRendererUrl = app.isPackaged
  ? pathToFileURL(path.join(__dirname, '../../renderer/index.html')).href
  : 'http://localhost:5173/';

// Ensure single instance lock
const isSingleInstance = app.requestSingleInstanceLock();
if (!isSingleInstance) {
  app.quit();
  process.exit(0);
}

let mainWindow: BrowserWindow | null = null;

async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1024,
    minHeight: 768,
    title: 'Kite Agentic Trading',
    titleBarStyle: 'hiddenInset',
    icon: app.isPackaged
      ? path.join(process.resourcesPath, 'icon.png')
      : path.join(__dirname, '../../../build/icon.png'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
    }
  });

  pythonBridge.setRenderer(mainWindow.webContents, trustedRendererUrl);
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!sameDocumentLocation(url, trustedRendererUrl)) event.preventDefault();
  });
  mainWindow.webContents.on('will-redirect', (event, url) => {
    if (!sameDocumentLocation(url, trustedRendererUrl)) event.preventDefault();
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url === 'https://developers.kite.trade/') void shell.openExternal(url);
    return { action: 'deny' };
  });

  const isDev = !app.isPackaged;
  if (isDev) {
    mainWindow.loadURL('http://localhost:5173');
    mainWindow.webContents.openDevTools();
  } else {
    mainWindow.loadFile(path.join(__dirname, '../../renderer/index.html'));
  }

  mainWindow.webContents.on('console-message', (_event, _level, message, line, _sourceId) => {
    console.log(`[Browser Console]: ${message} (line ${line})`);
  });

  mainWindow.on('closed', () => {
    pythonBridge.setRenderer(null);
    mainWindow = null;
  });
}

function setupMenu() {
  const isMac = process.platform === 'darwin';
  const template: any = [
    ...(isMac ? [{
      label: app.name,
      submenu: [
        { role: 'about' },
        { type: 'separator' },
        { role: 'services' },
        { type: 'separator' },
        { role: 'hide' },
        { role: 'hideOthers' },
        { role: 'unhide' },
        { type: 'separator' },
        { role: 'quit' }
      ]
    }] : []),
    {
      label: 'File',
      submenu: [
        isMac ? { role: 'close' } : { role: 'quit' }
      ]
    },
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' },
        { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' },
        { role: 'copy' },
        { role: 'paste' },
        ...(isMac ? [
          { role: 'pasteAndMatchStyle' },
          { role: 'delete' },
          { role: 'selectAll' },
          { type: 'separator' },
          {
            label: 'Speech',
            submenu: [
              { role: 'startSpeaking' },
              { role: 'stopSpeaking' }
            ]
          }
        ] : [
          { role: 'delete' },
          { type: 'separator' },
          { role: 'selectAll' }
        ])
      ]
    },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        { role: 'forceReload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' }
      ]
    },
    {
      label: 'Window',
      submenu: [
        { role: 'minimize' },
        { role: 'zoom' },
        ...(isMac ? [
          { type: 'separator' },
          { role: 'front' },
          { type: 'separator' },
          { role: 'window' }
        ] : [
          { role: 'close' }
        ])
      ]
    },
  ];

  const menu = Menu.buildFromTemplate(template);
  Menu.setApplicationMenu(menu);
}

app.whenReady().then(() => {
  setupIpcHandlers(() => mainWindow, trustedRendererUrl);
  setupMenu();
  createWindow();

  // Start the Python backend bridge
  pythonBridge.start();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('will-quit', () => {
  // Ensure we cleanly shut down the python process
  pythonBridge.stop();
});

// For single instance lock
app.on('second-instance', () => {
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  }
});
