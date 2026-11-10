#!/data/data/com.termux/files/usr/bin/sh
# Установка и обновление — одна и та же команда, в Termux:
#   curl -fsSL https://raw.githubusercontent.com/setsun-ai/phone-tether/main/install.sh | sh
# Повторный запуск = git pull + перезапуск. Из бота то же самое делает /update.
set -e
REPO_URL="https://github.com/setsun-ai/phone-tether.git"
DIR="$HOME/phone-tether"

main() {
  echo "==> пакеты"
  pkg update -y </dev/null
  pkg install -y git python iproute2 termux-api </dev/null

  echo "==> код"
  if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only; else git clone "$REPO_URL" "$DIR"; fi

  echo "==> автозапуск после перезагрузки"
  mkdir -p "$HOME/.tetherd" "$HOME/.termux/boot"
  printf '#!/data/data/com.termux/files/usr/bin/sh\nexec sh "%s/start-tetherd"\n' "$DIR" > "$HOME/.termux/boot/start-tetherd"
  chmod +x "$HOME/.termux/boot/start-tetherd"

  if [ ! -f "$HOME/.tetherd/config.json" ]; then
    echo "==> настройка"
    python "$DIR/tetherd.py" --setup </dev/tty
  fi

  echo "==> запуск"
  pkill -f "$DIR/tetherd.py" 2>/dev/null || true   # цикл в start-tetherd поднимет новую версию
  if ! pgrep -f "$DIR/start-tetherd" >/dev/null 2>&1; then
    nohup sh "$DIR/start-tetherd" >/dev/null 2>&1 &
  fi
  python "$DIR/tetherd.py" --check || true
  echo
  echo "Готово. Перенастроить бота: python $DIR/tetherd.py --setup"
}

main "$@"   # весь скрипт прочитан до запуска — безопасно для curl | sh
