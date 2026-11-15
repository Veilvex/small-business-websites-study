#!/bin/sh
# Звонилка v6: пускает коннектор через обычный 443 (nginx), с копией и откатом. VPN не трогаем.
# Запуск: sh ~/Ассистент/veilvex-calls/q6c.sh
cd ~/Ассистент/veilvex-calls || exit 1
ssh -i ~/.ssh/veilvex_vps root@94.20.58.6 'sh -s' <<'REMOTE' 2>&1 | tee /dev/tty | grep -v 'ССЫЛКА' > q6c.log
cd /opt/veilvex-calls
SECRET=$(grep '^INBOX_SECRET=' .env | cut -d= -f2)
PORT=$(grep '^INBOX_PORT=' .env | cut -d= -f2); PLAIN=$((PORT+20000))
BK=/root/nginx-backup-zvonok-$(date +%m%d%H%M).tgz
tar czf "$BK" /etc/nginx 2>/dev/null && echo "копия: $BK"
echo "--- nginx до (кратко):"
nginx -T 2>/dev/null | grep -n -E '^# configuration file|stream|http \{|listen|server_name|ssl_preread|map |proxy_pass|include' | grep -v -E 'mime|fastcgi|snippets' | head -60
restore() { echo "ОТКАТ"; rm -rf /etc/nginx && tar xzf "$BK" -C / && nginx -t 2>&1 | tail -1 && systemctl reload nginx; }
MAPF=$(grep -rl 'map[[:space:]]*\$ssl_preread_server_name' /etc/nginx/ 2>/dev/null | head -1)
if [ -n "$MAPF" ]; then
  MODE=stream
  grep -q 'n1.ourcases.com' "$MAPF" || sed -i "/map[[:space:]]*\$ssl_preread_server_name/a\\        n1.ourcases.com 127.0.0.1:$PORT;" "$MAPF"
  cat > /etc/nginx/conf.d/zvonok-n1.conf <<CONF
server { listen 80; server_name n1.ourcases.com; location / { proxy_pass http://127.0.0.1:$PLAIN; proxy_set_header Host \$host; } }
CONF
elif grep -rq 'ssl_preread' /etc/nginx/; then
  echo "ОШИБКА: ssl_preread без map, не трогаю. Пришли этот лог."; exit 0
else
  MODE=http
  cat > /etc/nginx/conf.d/zvonok-n1.conf <<CONF
server {
  listen 80; listen 443 ssl; server_name n1.ourcases.com;
  ssl_certificate /opt/veilvex-calls/inbox.crt; ssl_certificate_key /opt/veilvex-calls/inbox.key;
  location / { proxy_pass http://127.0.0.1:$PLAIN; proxy_set_header Host \$host; proxy_read_timeout 120s; }
}
CONF
  cp /opt/veilvex-calls/inbox.key /etc/nginx/zvonok-inbox.key; chmod 600 /etc/nginx/zvonok-inbox.key
  sed -i 's#/opt/veilvex-calls/inbox.key#/etc/nginx/zvonok-inbox.key#' /etc/nginx/conf.d/zvonok-n1.conf
fi
echo "режим: $MODE"
nginx -t 2>&1 | tail -2
nginx -t >/dev/null 2>&1 || { restore; exit 0; }
systemctl reload nginx; sleep 3
R=$(curl -sk -m 15 --resolve n1.ourcases.com:443:127.0.0.1 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}' "https://n1.ourcases.com/$SECRET/mcp")
echo "$R" | grep -q '"result"' && echo "проверка 443: OK" || { echo "проверка 443: НЕТ ($R)" | cut -c1-200; }
echo "nginx: $(systemctl is-active nginx), xray: $(systemctl is-active xray), звонилка: $(systemctl is-active zvonok)"
if [ "$(systemctl is-active nginx)" != active ] || [ "$(systemctl is-active xray)" != active ]; then restore; fi
echo "ССЫЛКА ДЛЯ КОННЕКТОРА: https://n1.ourcases.com/$SECRET/mcp"
REMOTE
echo ГОТОВО
