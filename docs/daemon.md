# Running desk-beep as a daemon

`local.desk-beep.plist` runs `desk_beep.py` as root at boot and restarts it if it dies. It's installed once, then switched on and off; "off" persists across reboots.

## Install (once)

```sh
sudo install -m 644 -o root -g wheel docs/local.desk-beep.plist /Library/LaunchDaemons/
sudo launchctl disable system/local.desk-beep   # start switched off
```

## On

```sh
sudo launchctl enable system/local.desk-beep
sudo launchctl bootstrap system /Library/LaunchDaemons/local.desk-beep.plist
```

## Off

```sh
sudo launchctl bootout system/local.desk-beep
sudo launchctl disable system/local.desk-beep
```

## Check

```sh
sudo launchctl print system/local.desk-beep | grep -E 'state|pid'
tail -f /var/log/desk-beep.log
```

## Uninstall

```sh
sudo launchctl bootout system/local.desk-beep
sudo rm /Library/LaunchDaemons/local.desk-beep.plist
```

## Notes

- The plist points at `/Users/sam-hq/projects/playground/desk-beep/desk_beep.py`; move the repo and you must update it and reinstall.
- After editing `desk_beep.py`, run Off then On to pick up the change.
