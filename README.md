# OVOS Shell companion

provides various bus APIs that integrate with ovos-shell
    
    - color scheme manager
    - notifications widgets
    - configuration provider  (settings UI)
    - brightness control  (night mode etc)
    

## Features

```javascript
{
  "gui": {
     "ovos-gui-plugin-shell-companion": {
       "default_brightness": 100,
       "auto_dim": false,
       "auto_nightmode": false
     }
  }
}
```

### AutoDim

autodim will lower the screen brightness after 60 seconds of inactivity, until the user interacts with the GUI or talks to the OVOS device

### Nightmode

nightmode will perform actions based on sunset/sunrise times

- the homescreen changes to a simple clock with white text on black background
- default brightness is reduced by 20%

TODO, spec nighttime actions:
- lower volume
- enable autodim

## DEPRECATION WARNING

> in **ovos-core version 0.0.7** the bus apis provided by this repo used to be several individual PHAL plugins

the following packages have been deprecated in favor of this repo:
- https://github.com/OpenVoiceOS/ovos-PHAL-plugin-dashboard   <- DEPRECATED, community maintained, no official replacement, [removed from ovos-shell](https://github.com/OpenVoiceOS/ovos-gui/pull/10)
- https://github.com/OpenVoiceOS/ovos-PHAL-plugin-configuration-provider <- now part of this repo
- https://github.com/OpenVoiceOS/ovos-PHAL-plugin-notification-widgets <- now part of this repo
- https://github.com/OpenVoiceOS/ovos-PHAL-plugin-brightness-control-rpi <- now part of this repo
- https://github.com/OpenVoiceOS/ovos-PHAL-plugin-color-scheme-manager <- now part of this repo
