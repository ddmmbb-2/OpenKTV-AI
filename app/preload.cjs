const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('ktvDesktop', {
  openPlayer: () => ipcRenderer.invoke('player:open'),
  closePlayer: () => ipcRenderer.invoke('player:close'),
  togglePlayerFullscreen: () => ipcRenderer.invoke('player:toggle-fullscreen'),
  getDisplays: () => ipcRenderer.invoke('screen:get-displays'),
  onPlayerClosed: callback => ipcRenderer.on('player:closed', callback),
  onPlayerOpened: callback => ipcRenderer.on('player:opened', callback),
});
