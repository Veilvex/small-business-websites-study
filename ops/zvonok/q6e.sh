#!/bin/sh
# Звонилка v6.2: обращение «Сэр»/«Мистер Арз» вперемешку, голос прежний.
# Запуск на Маке: sh ~/Ассистент/veilvex-calls/q6e.sh
cd ~/Ассистент/veilvex-calls || exit 1
SHA=${SHA:-main}
WANT=cbe741d3d3be338d6917daf378400f15
curl -fsSL "https://raw.githubusercontent.com/Veilvex/small-business-websites-study/$SHA/ops/zvonok/zvonok.py" -o zvonok.py.new || { echo "не скачалось"; exit 1; }
GOT=$(md5 -q zvonok.py.new 2>/dev/null || md5sum zvonok.py.new | cut -d' ' -f1)
[ "$GOT" = "$WANT" ] || { echo "файл не тот ($GOT)"; exit 1; }
scp -q -i ~/.ssh/veilvex_vps zvonok.py.new root@94.20.58.6:/opt/veilvex-calls/zvonok.py.new || { echo "scp не прошёл"; exit 1; }
ssh -i ~/.ssh/veilvex_vps root@94.20.58.6 'sh -s' <<'REMOTE' 2>&1 | tee q6e.log
cd /opt/veilvex-calls
U=$(systemctl show -p User --value zvonok); [ -n "$U" ] || U=root
cp zvonok.py zvonok.py.bak-v61
PY=$( [ -x venv/bin/python ] && echo venv/bin/python || echo python3 )
$PY -m py_compile zvonok.py.new || { echo "ошибка компиляции, оставляю старую"; exit 1; }
mv zvonok.py.new zvonok.py && chown "$U" zvonok.py
systemctl restart zvonok
sleep 25
echo "служба: $(systemctl is-active zvonok)"
journalctl -u zvonok --since '-40s' --no-pager -o cat | grep -i -E 'модель|live|коннектор|error|ошибк' | grep -v -i secret | cut -c1-200
REMOTE
echo ГОТОВО
