// The whole bridge: the renderer is the existing web app served by the
// backend; the only native thing it may do is post a system notification.
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('lunamothNative', {
  notify: (title, body) => ipcRenderer.invoke('lunamoth:notify', { title, body }),
})
