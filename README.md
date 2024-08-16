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
       "night_default_brightness": 70,
       "low_brightness": 20,
       "auto_dim_seconds": 60,
       "auto_dim": false,
       "auto_nightmode": false
     }
  }
}
```


### Night mode

night mode will perform actions based on sunset/sunrise times

- the homescreen changes to a simple clock with white text on black background
- default brightness is reduced
- auto-dim is enabled

brightness level during nighttime can be set via `"night_default_brightness"`

### Auto Dim

auto-dim will lower the screen brightness after 60 seconds of inactivity, until the user interacts with the GUI or talks to the OVOS device

brightness level when idle can be set via `"low_brightness"`

auto-dim can be enabled at all times by setting `"auto_dim": true` in your config


## DEPRECATION WARNING

> in **ovos-core version 0.0.7** the bus apis provided by this repo used to be several individual PHAL plugins

the following packages have been deprecated in favor of this repo:
- https://github.com/OpenVoiceOS/ovos-PHAL-plugin-dashboard   <- DEPRECATED, community maintained, no official replacement, [removed from ovos-shell](https://github.com/OpenVoiceOS/ovos-gui/pull/10)
- https://github.com/OpenVoiceOS/ovos-PHAL-plugin-configuration-provider <- now part of this repo
- https://github.com/OpenVoiceOS/ovos-PHAL-plugin-notification-widgets <- now part of this repo
- https://github.com/OpenVoiceOS/ovos-PHAL-plugin-brightness-control-rpi <- now part of this repo
- https://github.com/OpenVoiceOS/ovos-PHAL-plugin-color-scheme-manager <- now part of this repo
