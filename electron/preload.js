'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('flymoon', {
    getConfig:       ()      => ipcRenderer.invoke('get-config'),
    saveConfig:      (cfg)   => ipcRenderer.invoke('save-config', cfg),
    wizardComplete:  (cfg)   => ipcRenderer.invoke('wizard-complete', cfg),
    openExternal:    (url)   => ipcRenderer.invoke('open-external', url),
    getResourcesPath: ()     => ipcRenderer.invoke('get-resources-path'),
});
