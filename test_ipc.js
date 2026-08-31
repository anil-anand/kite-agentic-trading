const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');

app.whenReady().then(() => {
  const win = new BrowserWindow({
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false
    }
  });
  
  win.loadURL('data:text/html,<html><body><script>const {ipcRenderer} = require("electron"); ipcRenderer.invoke("auth:login", {apiKey: "test", apiSecret: "test"}).then(console.log).catch(console.error);</script></body></html>');
  
  // mock handlers just to see if it loads
  const { authManager } = require('./dist/main/main/auth-manager.js');
  ipcMain.handle('auth:login', async (_, creds) => {
    console.log("RECEIVED IN IPCMAIN", creds);
    return await authManager.startLogin(creds.apiKey, creds.apiSecret);
  });
});
