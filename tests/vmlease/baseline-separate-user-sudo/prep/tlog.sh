echo '== install tlog-rec per-distro (best-effort; failure -> L0 refuses, which is a separate finding) =='
. /etc/os-release
if command -v tlog-rec >/dev/null 2>&1; then
  echo 'tlog-rec already present'
else
  (
    case "${ID:-}" in
      ubuntu)
        sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y tlog
        ;;
      debian)
        maj=$(echo "${VERSION_ID:-0}" | cut -d. -f1)
        if [ "${maj:-0}" -le 12 ] 2>/dev/null; then
          sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y tlog
        else
          echo "debian ${VERSION_ID}: source-building tlog"
          sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
          sudo DEBIAN_FRONTEND=noninteractive apt-get install -y build-essential autoconf automake libtool m4 pkg-config git libjson-c-dev libsystemd-dev libcurl4-openssl-dev libutempter-dev
          T=$(mktemp -d)
          git clone --depth=1 https://github.com/Scribery/tlog "$T/tlog"
          cd "$T/tlog" && autoreconf -i -f && ./configure --prefix=/usr --sysconfdir=/etc --localstatedir=/var && make -j"$(nproc)" && sudo make install && sudo ldconfig
        fi
        ;;
      fedora)
        sudo dnf -y install tlog
        ;;
      arch|manjaro)
        if command -v paru >/dev/null 2>&1; then
          paru -S --noconfirm tlog
        elif command -v yay >/dev/null 2>&1; then
          yay -S --noconfirm tlog
        else
          sudo pacman -Sy --needed --noconfirm base-devel git
          T=$(mktemp -d)
          git clone --depth=1 https://aur.archlinux.org/yay.git "$T/yay"
          cd "$T/yay" && makepkg -si --noconfirm && yay -S --noconfirm tlog
        fi
        ;;
    esac
  ) || echo 'tlog install step returned non-zero'
fi
command -v tlog-rec >/dev/null 2>&1 && echo PREP_TLOG_OK || echo PREP_TLOG_MISSING_review
