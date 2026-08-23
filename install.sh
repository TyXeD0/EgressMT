#!/usr/bin/env bash
set -Eeuo pipefail

REPO="TyXeD0/EgressMT"
BRANCH="${EGRESSMT_BRANCH:-main}"
RAW="https://raw.githubusercontent.com/${REPO}/${BRANCH}"
TMP_DIR="${TMPDIR:-/tmp}/egressmt-installer"
LANG_FILE="/etc/egressmt/language"

# Piped installers do not normally own stdin. Reattach the user's terminal so
# the language selector and all subsequent menus remain interactive.
if [[ ! -t 0 && -r /dev/tty ]]; then
    exec </dev/tty
fi

red(){ printf '\033[31m%s\033[0m\n' "$*" >&2; }
green(){ printf '\033[32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$*"; }
bold(){ printf '\033[1m%s\033[0m\n' "$*"; }

choose_language(){
    if [[ "${EGRESSMT_LANG:-}" == "ru" || "${EGRESSMT_LANG:-}" == "en" ]]; then
        LANG_CODE="$EGRESSMT_LANG"
        return
    fi
    echo
    echo "EgressMT"
    echo "========"
    echo "1) Русский"
    echo "2) English"
    while true; do
        printf "> "
        read -r ans
        case "$ans" in
            1|ru|RU) LANG_CODE="ru"; break ;;
            2|en|EN) LANG_CODE="en"; break ;;
            *) echo "1 / 2" ;;
        esac
    done
}

trm(){
    case "$LANG_CODE:$1" in
        ru:root) echo "Запустите установщик через sudo/root.";; en:root) echo "Run the installer with sudo/root.";;
        ru:title) echo "EgressMT — установка и управление";; en:title) echo "EgressMT — setup and management";;
        ru:intro1) echo "Входной VPS — сервер с MTProxyL/Telemt, откуда Telegram недоступен или работает нестабильно.";;
        en:intro1) echo "The entry VPS is the MTProxyL/Telemt server where Telegram is unavailable or unreliable.";;
        ru:intro2) echo "Выходные ноды — удалённые VPS, у которых есть доступ к Telegram. EgressMT будет переключаться между ними автоматически.";;
        en:intro2) echo "Egress nodes are remote VPS instances that can reach Telegram. EgressMT can fail over between them automatically.";;
        ru:unsupported) echo "Версия v0.1.0-rc1 рассчитана на Ubuntu 24.04 LTS.";; en:unsupported) echo "v0.1.0-rc1 targets Ubuntu 24.04 LTS.";;
        ru:continue) echo "Продолжить всё равно? [y/N]: ";; en:continue) echo "Continue anyway? [y/N]: ";;
        ru:menu1) echo "Полная установка / настройка";; en:menu1) echo "Full install / setup";;
        ru:menu2) echo "Установить MTProxyL (если ещё не установлен)";; en:menu2) echo "Install MTProxyL (if missing)";;
        ru:menu3) echo "Установить или обновить EgressMT Core";; en:menu3) echo "Install or update EgressMT Core";;
        ru:menu4) echo "Управление выходными нодами";; en:menu4) echo "Manage egress nodes";;
        ru:menu5) echo "Установить или обновить интеграцию веб-панели";; en:menu5) echo "Install or update web-panel integration";;
        ru:menu6) echo "Показать статус";; en:menu6) echo "Show status";;
        ru:menu7) echo "Удалить EgressMT с входного VPS";; en:menu7) echo "Uninstall EgressMT from the entry VPS";;
        ru:menu0) echo "Выход";; en:menu0) echo "Exit";;
        ru:installed) echo "MTProxyL уже установлен.";; en:installed) echo "MTProxyL is already installed.";;
        ru:mtinstall) echo "Запускаю официальный установщик MTProxyL. Завершите его мастер настройки, затем вернитесь в EgressMT.";;
        en:mtinstall) echo "Starting the official MTProxyL installer. Complete its setup wizard, then return to EgressMT.";;
        ru:needproxy) echo "Сначала необходимо установить и настроить MTProxyL/Telemt на входном VPS.";; en:needproxy) echo "MTProxyL/Telemt must be installed and configured on the entry VPS first.";;
        ru:firstexit) echo "Добавить первую выходную ноду сейчас? [Y/n]: ";; en:firstexit) echo "Add the first egress node now? [Y/n]: ";;
        ru:panelask) echo "Установить интеграцию EgressMT в MTProxyL Panel? [Y/n]: ";; en:panelask) echo "Install EgressMT integration into MTProxyL Panel? [Y/n]: ";;
        ru:done) echo "Готово.";; en:done) echo "Done.";;
        ru:press) echo "Нажмите Enter для продолжения...";; en:press) echo "Press Enter to continue...";;
        ru:confirmremove) echo "Удалить EgressMT с входного VPS? Удалённые выходные ноды автоматически изменяться не будут. [y/N]: ";;
        en:confirmremove) echo "Remove EgressMT from the entry VPS? Remote egress nodes will not be changed automatically. [y/N]: ";;
        *) echo "$1";;
    esac
}

pause(){ echo; read -r -p "$(trm press)" _ || true; }
yes_answer(){ [[ "${1,,}" == "y" || "${1,,}" == "yes" || "${1,,}" == "д" || "${1,,}" == "да" ]]; }
no_answer(){ [[ "${1,,}" == "n" || "${1,,}" == "no" || "${1,,}" == "н" || "${1,,}" == "нет" ]]; }

need_root(){
    if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
        red "$(trm root)"
        echo "curl -fsSL ${RAW}/install.sh | sudo bash"
        exit 1
    fi
    install -d -m 755 /etc/egressmt
    printf '%s\n' "$LANG_CODE" >"$LANG_FILE"
    chmod 644 "$LANG_FILE"
}

check_os(){
    [[ -r /etc/os-release ]] || return 0
    . /etc/os-release
    if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
        yellow "$(trm unsupported)"
        printf '%s' "$(trm continue)"
        read -r a
        yes_answer "$a" || exit 0
    fi
}

download(){
    local path="$1" dest="$2"
    mkdir -p "$(dirname "$dest")"
    curl -fsSL --retry 4 --retry-delay 2 --retry-all-errors "${RAW}/${path}" -o "$dest"
}

install_mtproxyl(){
    if command -v mtproxyl >/dev/null 2>&1; then
        green "$(trm installed)"
        mtproxyl version 2>/dev/null || true
        return 0
    fi
    yellow "$(trm mtinstall)"
    mkdir -p "$TMP_DIR"
    curl -fsSL --retry 4 --retry-delay 2 --retry-all-errors \
        https://raw.githubusercontent.com/Liafanx/MTProxyL/main/install.sh \
        -o "$TMP_DIR/mtproxyl-install.sh"
    bash "$TMP_DIR/mtproxyl-install.sh"
    command -v mtproxyl >/dev/null 2>&1 || { red "$(trm needproxy)"; return 1; }
}

telemt_ready(){
    command -v mtproxyl >/dev/null 2>&1 || return 1
    [[ -f /opt/mtproxyl/mtproxy/config.toml ]] && return 0
    command -v docker >/dev/null 2>&1 && docker inspect mtproxyl >/dev/null 2>&1 && return 0
    return 1
}

install_core(){
    telemt_ready || { red "$(trm needproxy)"; return 1; }
    mkdir -p "$TMP_DIR"
    download "lib/install-core.sh" "$TMP_DIR/install-core.sh"
    chmod 700 "$TMP_DIR/install-core.sh"
    EGRESSMT_LANG="$LANG_CODE" EGRESSMT_BRANCH="$BRANCH" bash "$TMP_DIR/install-core.sh"
}

manage_nodes(){
    [[ -x /usr/local/bin/egressmt-menu ]] || { red "EgressMT Core is not installed"; return 1; }
    EGRESSMT_LANG="$LANG_CODE" /usr/local/bin/egressmt-menu
}

install_panel(){
    telemt_ready || { red "$(trm needproxy)"; return 1; }
    [[ -x /usr/local/bin/egressmt ]] || install_core
    mkdir -p "$TMP_DIR"
    download "panel/install.sh" "$TMP_DIR/install-panel.sh"
    chmod 700 "$TMP_DIR/install-panel.sh"
    EGRESSMT_LANG="$LANG_CODE" EGRESSMT_BRANCH="$BRANCH" bash "$TMP_DIR/install-panel.sh"
}

full_install(){
    install_mtproxyl
    telemt_ready || { red "$(trm needproxy)"; return 1; }
    install_core

    printf '%s' "$(trm firstexit)"
    read -r first
    if [[ -z "$first" ]] || ! no_answer "$first"; then
        manage_nodes
    fi

    printf '%s' "$(trm panelask)"
    read -r p
    if [[ -z "$p" ]] || ! no_answer "$p"; then
        install_panel
    fi
    green "$(trm done)"
}

show_status(){
    echo
    if command -v egressmt >/dev/null 2>&1; then
        egressmt status || true
    else
        echo "EgressMT: not installed"
    fi
    echo
    systemctl --no-pager --full status mtproxyl-egressd.service 2>/dev/null | sed -n '1,12p' || true
}

uninstall_egressmt(){
    printf '%s' "$(trm confirmremove)"
    read -r a
    yes_answer "$a" || return 0
    mkdir -p "$TMP_DIR"
    download "lib/uninstall.sh" "$TMP_DIR/uninstall.sh"
    chmod 700 "$TMP_DIR/uninstall.sh"
    EGRESSMT_LANG="$LANG_CODE" bash "$TMP_DIR/uninstall.sh"
}

menu(){
    while true; do
        clear 2>/dev/null || true
        bold "$(trm title)"
        echo "v0.1.0-rc1"
        echo
        echo "$(trm intro1)"
        echo "$(trm intro2)"
        echo
        echo "1) $(trm menu1)"
        echo "2) $(trm menu2)"
        echo "3) $(trm menu3)"
        echo "4) $(trm menu4)"
        echo "5) $(trm menu5)"
        echo "6) $(trm menu6)"
        echo "7) $(trm menu7)"
        echo "0) $(trm menu0)"
        echo
        printf "> "
        read -r choice
        case "$choice" in
            1) full_install; pause ;;
            2) install_mtproxyl; pause ;;
            3) install_core; pause ;;
            4) manage_nodes; pause ;;
            5) install_panel; pause ;;
            6) show_status; pause ;;
            7) uninstall_egressmt; pause ;;
            0) exit 0 ;;
            *) sleep 1 ;;
        esac
    done
}

choose_language
need_root
check_os
command -v curl >/dev/null 2>&1 || { apt-get update -y; apt-get install -y curl ca-certificates; }
menu
