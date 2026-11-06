#!/usr/bin/env bash
# CanBot Ubuntu kurulumu (sunucu / VPS). Kurar ve 7/24 arka planda çalıştırır.
#
# Kaynak kodu iki yoldan alabilir:
#  1) Bu dosya CanBot klasörünün içinden çalıştırılırsa (ör. GitHub'dan indirilen ZIP
#     sunucuya yüklenip açıldıysa) o klasörden kurar:
#        sudo bash kurulum-ubuntu.sh
#  2) Aksi halde GitHub'dan indirir. Depo gizli (private) ise salt okunur bir GitHub
#     token'ı GH_TOKEN ile verilir:
#        sudo GH_TOKEN=github_pat_xxx bash kurulum-ubuntu.sh
#
# Güncellemek için aynı komutu tekrar çalıştırın (ayarlar, anahtarlar ve kayıtlar korunur).
# Panel güvenlik için yalnızca sunucunun kendisine (127.0.0.1) açılır; erişim için SSH tüneli
# ya da Tailscale kullanılır (kurulum sonunda yazdırılır).
set -euo pipefail

REPO="${CANBOT_REPO:-https://github.com/g2w4ypjjrp-lang/Can.git}"
BRANCH="${CANBOT_BRANCH:-claude/merhaba-fpsls2}"
DIR="${CANBOT_DIR:-/opt/canbot}"
PORT="${CANBOT_PORT:-8765}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Lütfen komutu sudo ile çalıştırın." >&2
  exit 1
fi

echo "==> Gerekli paketler kuruluyor"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -qq
apt-get install -y -qq python3 python3-venv git curl ca-certificates >/dev/null

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/run.py" ] && [ -d "$SCRIPT_DIR/bot" ] && [ "$SCRIPT_DIR" != "$DIR" ]; then
  echo "==> CanBot bu klasörden kuruluyor: $SCRIPT_DIR"
  SRC="$SCRIPT_DIR"
else
  echo "==> CanBot GitHub'dan indiriliyor ($BRANCH)"
  AUTH=()
  if [ -n "${GH_TOKEN:-}" ]; then
    AUTH=(-c "http.extraHeader=Authorization: Basic $(printf 'x-access-token:%s' "$GH_TOKEN" | base64 -w0)")
  fi
  if ! git "${AUTH[@]}" clone -q --depth 1 -b "$BRANCH" "$REPO" "$TMP/src"; then
    echo "HATA: GitHub'dan indirilemedi. Depo gizliyse GH_TOKEN ile salt okunur bir token verin" >&2
    echo "      ya da depoyu ZIP olarak indirip sunucuda açın ve bu dosyayı o klasörden çalıştırın." >&2
    exit 1
  fi
  SRC="$TMP/src"
fi

echo "==> Dosyalar $DIR klasörüne kopyalanıyor (ayarlar korunur)"
mkdir -p "$DIR"
(cd "$SRC" && tar --exclude=./.venv --exclude=./data --exclude=./.git -cf - .) | (cd "$DIR" && tar -xf -)

echo "==> Python ortamı hazırlanıyor"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

if ! id canbot >/dev/null 2>&1; then
  useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin canbot
fi
mkdir -p "$DIR/data"
chown -R canbot:canbot "$DIR"

echo "==> Arka plan hizmeti (systemd) kuruluyor"
cat > /etc/systemd/system/canbot.service <<SERVICE
[Unit]
Description=CanBot otomatik islem botu
After=network-online.target
Wants=network-online.target

[Service]
User=canbot
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python run.py --no-browser --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SERVICE
systemctl daemon-reload
systemctl enable -q canbot
systemctl restart canbot

echo "==> Kontroller"
ok=""
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
if [ -n "$ok" ]; then echo "    CanBot çalışıyor."; else echo "    UYARI: CanBot başlamadı. Ayrıntı: journalctl -u canbot -n 50"; fi

code=$(curl -s -o /dev/null -m 15 -w "%{http_code}" https://fapi.binance.com/fapi/v1/ping || true)
case "$code" in
  200) echo "    Binance vadeli sunucusuna erişim: TAMAM" ;;
  451) echo "    UYARI: Binance bu sunucunun bulunduğu ülkeden erişime izin vermiyor (HTTP 451). Başka bir konum seçin." ;;
  *)   echo "    UYARI: Binance'e ulaşılamadı (kod: ${code:-yok}). Sunucunun internet bağlantısını kontrol edin." ;;
esac
ip=$(curl -4 -s -m 10 https://api.ipify.org || true)

cat <<INFO

=====================================================================
 Kurulum tamam.
 Sunucunun internet IP adresi: ${ip:-bilinmiyor}
   -> Binance API anahtarının "güvenilir IP" listesine bu adresi ekleyin.

 Panele bilgisayarınızdan bağlanmak için (Windows'ta PowerShell'de):
   ssh -L $PORT:127.0.0.1:$PORT KULLANICI@${ip:-SUNUCU_IP}
 Bu pencere açıkken tarayıcıda açın:  http://localhost:$PORT
 İlk açılışta panel şifresi belirlersiniz, sonra kurulum sihirbazı gelir.

 Telefondan bağlanmak için (isteğe bağlı, Tailscale):
   curl -fsSL https://tailscale.com/install.sh | sh
   tailscale up            (çıkan bağlantıyla giriş yapın)
   tailscale serve --bg $PORT
 Telefona Tailscale uygulamasını kurup aynı hesapla girin; panel adresi
   "tailscale serve status" komutunun gösterdiği https://...ts.net adresidir.

 Faydalı komutlar:
   systemctl status canbot        durum
   journalctl -u canbot -f        canlı kayıtlar
   systemctl restart canbot       yeniden başlat
=====================================================================
INFO
