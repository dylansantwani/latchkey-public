'use strict';

// Everything the panel may ask for, and nothing else. The renderer reads the API
// itself, so the only bridge it needs is window state: which port to read, whether
// clicks are being captured, and how to quit. No filesystem, no shell, no way for
// anything in the page to reach the agent.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('viewer', {
  // Returns { port, clickThrough, server } - `server` is the main process's own
  // startup probe of 127.0.0.1:<port>, which is the only place a refused connection
  // can be told apart from a blocked one.
  getConfig: () => ipcRenderer.invoke('viewer:config'),
  // Transient: true while the pointer is over the control strip, so those clicks stop
  // being forwarded to the page underneath.
  setInteractive: (on) => ipcRenderer.send('viewer:interactive', !!on),
  toggleClickThrough: () => ipcRenderer.invoke('viewer:toggle-click-through'),
  // Asks the window to take the shape of the page: the frame's aspect plus the height of
  // the strip and the meta line above it. Without it the window keeps whatever height it
  // was given, and the difference is a band of nothing above and below the picture.
  fit: (size) => ipcRenderer.send('viewer:fit', size),
  quit: () => ipcRenderer.send('viewer:quit'),
});
