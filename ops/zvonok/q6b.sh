#!/bin/sh
# Звонилка v6: исправляет доступ к ключу коннектора. Запуск: sh ~/Ассистент/veilvex-calls/q6b.sh
cd ~/Ассистент/veilvex-calls || exit 1
ssh -i ~/.ssh/veilvex_vps root@94.20.58.6 'sh -s' <<'REMOTE' 2>&1 | tee q6b.log
cd /opt/veilvex-calls
U=$(systemctl show -p User --value zvonok); [ -n "$U" ] || U=root
chown "$U" inbox.key inbox.crt && chmod 600 inbox.key && chmod 644 inbox.crt
systemctl restart zvonok
sleep 20
echo "пользователь службы: $U"
echo "служба: $(systemctl is-active zvonok)"
P=$(grep '^INBOX_PORT=' .env | cut -d= -f2)
ss -ltn | grep -q ":$P " && echo "порт $P: слушает" || echo "порт $P: НЕ слушает"
journalctl -u zvonok -n 6 --no-pager -o cat | grep -i -E 'коннектор|error' | cut -c1-160
REMOTE
echo ГОТОВО
