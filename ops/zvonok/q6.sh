#!/bin/sh
# Звонилка v6: сама работает как Telegram-коннектор. Запуск: sh ~/Ассистент/veilvex-calls/q6.sh
cd ~/Ассистент/veilvex-calls || exit 1
{
set -e
curl -fsSL https://raw.githubusercontent.com/Veilvex/small-business-websites-study/5e1f62f/ops/zvonok/zvonok.py -o zvonok.py.new
[ "$(md5 -q zvonok.py.new)" = 6675b268b21e127b5146af79dcdca387 ] || { echo "ОШИБКА: файл не тот"; exit 1; }
scp -q -i ~/.ssh/veilvex_vps zvonok.py.new root@94.20.58.6:/opt/veilvex-calls/zvonok.py.new
ssh -i ~/.ssh/veilvex_vps root@94.20.58.6 'sh -s' <<'REMOTE'
set -e
cd /opt/veilvex-calls
grep -q '^INBOX_SECRET=' .env || echo "INBOX_SECRET=$(openssl rand -hex 16)" >> .env
PORT=$(grep '^INBOX_PORT=' .env | cut -d= -f2)
if [ -z "$PORT" ]; then
  for p in 2096 2087 2083 2053 8443; do ss -ltn | grep -q ":$p " || { PORT=$p; break; }; done
  [ -n "$PORT" ] || { echo "ОШИБКА: все порты заняты"; exit 1; }
  echo "INBOX_PORT=$PORT" >> .env
fi
[ -f inbox.crt ] || openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=n1.ourcases.com" -keyout inbox.key -out inbox.crt 2>/dev/null
chmod 600 inbox.key .env
cp zvonok.py zvonok.py.bak
venv/bin/python -m py_compile zvonok.py.new && mv zvonok.py.new zvonok.py
systemctl restart zvonok
sleep 20
echo "служба: $(systemctl is-active zvonok)"
ss -ltn | grep -q ":$PORT " && echo "порт $PORT: слушает" || echo "порт $PORT: НЕ слушает"
echo "--- фаервол (только смотрю):"
(ufw status 2>/dev/null | head -6) || true
(iptables -S INPUT 2>/dev/null | head -6) || true
echo "--- лог:"
journalctl -u zvonok -n 12 --no-pager -o cat | cut -c1-160
echo "ССЫЛКА ДЛЯ КОННЕКТОРА: https://n1.ourcases.com:$PORT/$(grep '^INBOX_SECRET=' .env | cut -d= -f2)/mcp"
REMOTE
echo ГОТОВО
} 2>&1 | tee /dev/tty | grep -v 'ССЫЛКА' > q6.log
