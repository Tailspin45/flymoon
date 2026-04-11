'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('zipcatcher', {
    getConfig:        ()      => ipcRenderer.invoke('get-config'),
    saveConfig:       (cfg)   => ipcRenderer.invoke('save-config', cfg),
    wizardComplete:   (cfg)   => ipcRenderer.invoke('wizard-complete', cfg),
    wizardSkipAll:    ()      => ipcRenderer.invoke('wizard-skip-all'),
    ipGeolocate:      ()      => ipcRenderer.invoke('ip-geolocate'),
    openPreferences:  ()      => ipcRenderer.invoke('open-preferences'),
    openExternal:     (url)   => ipcRenderer.invoke('open-external', url),
    getResourcesPath: ()      => ipcRenderer.invoke('get-resources-path'),
    onLaunchProgress: (cb)    => ipcRenderer.on('launch-progress', (_, msg) => cb(msg)),
});
