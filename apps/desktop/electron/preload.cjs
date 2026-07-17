// The whole bridge: the renderer is the existing web app served by the
// backend; the only native thing it may do is post a system notification.
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('charaNative', {
  notify: (title, body) => ipcRenderer.invoke('chara:notify', { title, body }),
})
