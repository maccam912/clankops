# systemd (Telegram daemon)

This project can be run as a long-lived daemon via systemd.

## Install and start

```bash
sudo cp /root/clankops/deploy/systemd/clankops-telegram.service /etc/systemd/system/clankops-telegram.service
sudo systemctl daemon-reload
sudo systemctl enable --now clankops-telegram.service
```

## Check status and logs

```bash
systemctl status clankops-telegram.service
journalctl -u clankops-telegram.service -f
```

## Customize flags (example: add --dangerzone later)

Create a drop-in:

```bash
sudo systemctl edit clankops-telegram.service
```

Then add:

```ini
[Service]
Environment="CLANKOPS_ARGS=--telegram --dangerzone"
```

Apply:

```bash
sudo systemctl daemon-reload
sudo systemctl restart clankops-telegram.service
```
