#!/usr/bin/env bash
# 闲云期货管理脚本：Debian/Ubuntu、Python 3.10+、systemd。
# 仅管理 futures-web.service / futures-worker.service；不管理其他项目。
set -uo pipefail
umask 077

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)"
if [[ -n "${FUTURES_HOME:-}" ]]; then
    FUTURES_ROOT="$FUTURES_HOME"
elif [[ -f "$SCRIPT_DIR/.futures-owned" ]]; then
    FUTURES_ROOT="$SCRIPT_DIR"
else
    FUTURES_ROOT="/root/futures"
fi
VENV=""
PYTHON=""
SYSTEM_PYTHON=""
SOURCE_DIR=""
TEMP_DIRS=()
ACTIVE_BEFORE=()
RECOVERY_PENDING=0
PROXY_RESCUE=""
PROXY_CHANGED=0
EXIT_AFTER_ACTION=0
CODE_RESCUE=""
CODE_CHANGED=0
NEW_UNITS=()
UNITS=(futures-web.service futures-worker.service)
CODE_FILES=(
    app.py backup.py backtest.py check_backtest.py continuous.py config.py contracts.py gunicorn_conf.py manage.py
    nginx_proxy.py firewall.py
    market_data.py push.py push_format.py runtime.py service.py storage.py strategy.py requirements.txt
    templates/base.html templates/login.html templates/status.html templates/system.html
    templates/backtest.html templates/_live.html templates/_history.html templates/_labels.html
    static/portal.css static/portal.js static/login.webp
)

say() { printf '%s\n' "$*"; }
fail() { printf '错误：%s\n' "$*" >&2; return 1; }

# Colors are limited to an interactive stdout. NO_COLOR=1 forces plain text,
# so SSH and Windows terminals without ANSI support remain fully usable.
UI_RESET='' UI_BLUE='' UI_CYAN='' UI_GREEN='' UI_RED='' UI_YELLOW='' UI_MAGENTA='' UI_DIM=''
if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-}" != dumb ]]; then
    UI_RESET=$'\033[0m'; UI_BLUE=$'\033[1;94m'; UI_CYAN=$'\033[1;96m'
    UI_GREEN=$'\033[1;92m'; UI_RED=$'\033[1;91m'; UI_YELLOW=$'\033[1;93m'
    UI_MAGENTA=$'\033[1;95m'; UI_DIM=$'\033[2m'
fi
ui_text() { printf '%s%s%s\n' "${1:-}" "${2:-}" "$UI_RESET"; }
ui_title() {
    printf '\n%s%s%s\n' "$UI_CYAN" '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━' "$UI_RESET"
    printf '%s  %s%s\n' "$UI_BLUE" "$1" "$UI_RESET"
    printf '%s%s%s\n' "$UI_CYAN" '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━' "$UI_RESET"
}
ui_item() { printf '  %s%2s)%s  %s\n' "$2" "$1" "$UI_RESET" "$3"; }
ui_success() { ui_text "$UI_GREEN" "✔ $*"; }
ui_warning() { ui_text "$UI_YELLOW" "⚠ $*"; }
ui_error() { ui_text "$UI_RED" "✘ $*"; }


setup_input() {
    if [[ -t 0 ]]; then
        # SSH clients can transmit BS (^H) or DEL (^?). Readline supports both.
        stty erase '^?' 2>/dev/null || true
        bind '"\C-h": backward-delete-char' 2>/dev/null || true
        bind '"\177": backward-delete-char' 2>/dev/null || true
        bind '"\e[3~": delete-char' 2>/dev/null || true
    fi
}

prompt() {
    local __f_name="$1" __f_label="$2" __f_default="${3-}" __f_value="" __f_text=""
    __f_text="$__f_label"
    [[ -z "$__f_default" ]] || __f_text+=" [$__f_default]"
    __f_text+=": "
    if [[ -t 0 ]]; then
        setup_input
        IFS= read -e -r -p "$__f_text" __f_value || return 1
    else
        printf '%s' "$__f_text" >&2
        IFS= read -r __f_value || return 1
    fi
    [[ -n "$__f_value" ]] || __f_value="$__f_default"
    printf -v "$__f_name" '%s' "$__f_value"
}

# The VPS console deliberately displays typed password characters like other
# editable fields. Passwords are still piped only to the local process stdin.
prompt_secret() { prompt "$1" "$2" ""; }

confirm_no() {
    local answer=""
    while true; do
        prompt answer "$1 请输入 y/n（回车默认 n）" "" || return 1
        case "$answer" in
            y|Y) return 0 ;;
            ''|n|N) return 1 ;;
            *) say '请输入 y 或 n。' ;;
        esac
    done
}

init_paths() {
    [[ "$FUTURES_ROOT" =~ ^/[A-Za-z0-9_./-]+$ ]] || { fail '项目目录须为不含空格的绝对路径。'; return 1; }
    [[ "$FUTURES_ROOT" != *'/../'* && "$FUTURES_ROOT" != */.. && "$FUTURES_ROOT" != *'/./'* && "$FUTURES_ROOT" != */. ]] || {
        fail '项目目录不能包含 . 或 .. 路径段。'; return 1;
    }
    case "${FUTURES_ROOT,,}" in
        /etc|/etc/*|/usr|/usr/*|/bin|/bin/*|/sbin|/sbin/*|/lib|/lib/*|/lib64|/lib64/*|/boot|/boot/*|/efi|/efi/*|/dev|/dev/*|/proc|/proc/*|/sys|/sys/*|/run|/run/*)
            fail '拒绝使用受保护的系统目录。'; return 1 ;;
        */quant|*/quant/*|*/nginx|*/nginx/*)
            fail '不能使用 quant、nginx 或它们的子目录。'; return 1 ;;
    esac
    local leaf="${FUTURES_ROOT##*/}" parent="$FUTURES_ROOT" canonical
    [[ "$leaf" =~ ^futures([-_][A-Za-z0-9_.-]+)?$ ]] || { fail '请使用独立的 futures 或 futures-名称 目录。'; return 1; }
    while [[ "$parent" != / ]]; do
        [[ ! -L "$parent" ]] || { fail "路径包含符号链接：$parent"; return 1; }
        parent="$(dirname -- "$parent")"
    done
    canonical="$(realpath -m -- "$FUTURES_ROOT")" || return 1
    [[ "$canonical" == "$FUTURES_ROOT" ]] || { fail '请使用规范的项目绝对路径。'; return 1; }
    [[ ! -e "$FUTURES_ROOT" || -d "$FUTURES_ROOT" ]] || { fail '项目路径已被普通文件占用。'; return 1; }
    local child
    for child in data backup .venv .futures-owned initial_credentials.txt; do
        [[ ! -L "$FUTURES_ROOT/$child" ]] || { fail "项目的 $child 不能是符号链接。"; return 1; }
    done
    VENV="$FUTURES_ROOT/.venv"
    PYTHON="$VENV/bin/python"
}

require_root() { [[ "$(id -u)" == 0 ]] || { fail '请使用 root 执行管理操作。'; return 1; }; }
require_systemd() {
    command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]] || {
        fail '本脚本需要 systemd；请在 Debian/Ubuntu VPS 内运行。'; return 1;
    }
}

require_debian() {
    [[ -r /etc/debian_version ]] && command -v apt-get >/dev/null 2>&1 || {
        fail '自动安装目前支持 Debian/Ubuntu。'; return 1;
    }
}

need_runtime() {
    [[ -x "$PYTHON" && -f "$FUTURES_ROOT/manage.py" && -f "$FUTURES_ROOT/data/runtime.sqlite3" ]] || {
        fail '请先执行菜单 1 完成安装。'; return 1;
    }
}

admin() { "$PYTHON" -B "$FUTURES_ROOT/manage.py" --root "$FUTURES_ROOT" "$@"; }
backup_cli() { "$PYTHON" -B "$FUTURES_ROOT/backup.py" "$@" --root "$FUTURES_ROOT"; }
proxy_cli() { "$PYTHON" -B "$FUTURES_ROOT/nginx_proxy.py" "$@" --root "$FUTURES_ROOT"; }
firewall_cli() { "$PYTHON" -B "$FUTURES_ROOT/firewall.py" "$@" --root "$FUTURES_ROOT"; }

capture_proxy() {
    new_temp PROXY_RESCUE || return 1
    mkdir -p "$PROXY_RESCUE/confs"
    local config name
    for config in /etc/nginx/conf.d/futures-portal.conf /etc/nginx/sites-enabled/futures-portal.conf; do
        name="$(dirname "$config" | tr / _)"
        if [[ -f "$config" && ! -L "$config" ]]; then
            cp -a -- "$config" "$PROXY_RESCUE/confs/${name}-futures-portal.conf" || return 1
        fi
    done
    if [[ -f "$FUTURES_ROOT/data/nginx_proxy.json" && ! -L "$FUTURES_ROOT/data/nginx_proxy.json" ]]; then
        cp -a -- "$FUTURES_ROOT/data/nginx_proxy.json" "$PROXY_RESCUE/state.json" || return 1
    fi
    PROXY_CHANGED=1
}

restore_proxy() {
    [[ "$PROXY_CHANGED" == 1 && -n "$PROXY_RESCUE" ]] || return 0
    local config name original
    for config in /etc/nginx/conf.d/futures-portal.conf /etc/nginx/sites-enabled/futures-portal.conf; do
        name="$(dirname "$config" | tr / _)"
        original="$PROXY_RESCUE/confs/${name}-futures-portal.conf"
        if [[ -f "$original" ]]; then
            cp -a -- "$original" "$config" || return 1
        elif [[ -f "$config" && ! -L "$config" ]] && grep -Fqx -- '# FUTURES-PORTAL MANAGED CONFIG' <(head -n 1 "$config"); then
            rm -f -- "$config" || return 1
        fi
    done
    if [[ -f "$PROXY_RESCUE/state.json" ]]; then
        cp -a -- "$PROXY_RESCUE/state.json" "$FUTURES_ROOT/data/nginx_proxy.json" || return 1
    else
        rm -f -- "$FUTURES_ROOT/data/nginx_proxy.json" || return 1
    fi
    if command -v nginx >/dev/null 2>&1 && nginx -t >/dev/null 2>&1; then
        nginx -s reload >/dev/null 2>&1 || say '警告：原 Nginx 配置已恢复，但热重载失败，请手动核对。'
    fi
    PROXY_CHANGED=0
}

existing_domain() {
    proxy_cli status | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin).get("domain") or "dism.eu.org")'
}

ask_domain_cert() {
    local __domain_var="$1" __cert_var="$2" __key_var="$3" default="$4"
    local entered_domain entered_cert="" entered_key="" result found
    prompt entered_domain '访问域名（不带 https:// 或端口）' "$default" || return 1
    [[ -n "$entered_domain" ]] || { fail '请填写域名，例如 dism.eu.org。'; return 1; }
    result="$(proxy_cli probe --domain "$entered_domain")" || return 1
    found="$(printf '%s\n' "$result" | "$PYTHON" -c 'import json,sys; print("yes" if json.load(sys.stdin).get("found") else "no")')" || return 1
    if [[ "$found" == yes ]]; then
        say '已经检测到域名匹配、私钥匹配的证书，将开启 HTTPS。'
    else
        say '没有自动找到匹配证书。如实际已有证书，可输入 fullchain.pem 与 privkey.pem 绝对路径。'
        prompt entered_cert '证书完整链文件（回车使用 HTTP）' '' || return 1
        if [[ -n "$entered_cert" ]]; then
            prompt entered_key '证书私钥文件' '' || return 1
            [[ -n "$entered_key" ]] || { fail '手动输入证书路径时必须提供私钥。'; return 1; }
        fi
    fi
    printf -v "$__domain_var" '%s' "$entered_domain"
    printf -v "$__cert_var" '%s' "$entered_cert"
    printf -v "$__key_var" '%s' "$entered_key"
}

probe_public() {
    [[ -f "$FUTURES_ROOT/data/nginx_proxy.json" ]] || return 0
    proxy_cli health || { fail 'Nginx 代理健康检查未通过；请查看菜单 4 或 6。'; return 1; }
}


new_temp() {
    local __f_target="$1" __f_temp
    __f_temp="$(mktemp -d /tmp/futures-manager.XXXXXXXX)" || return 1
    TEMP_DIRS+=("$__f_temp")
    printf -v "$__f_target" '%s' "$__f_temp"
}

unit_exists() {
    local load_state
    load_state="$(systemctl show "$1" -p LoadState --value 2>/dev/null)" || return 1
    [[ -n "$load_state" && "$load_state" != not-found ]]
}

check_unit_ownership() {
    local unit fragment unit_file
    for unit in "${UNITS[@]}"; do
        unit_file="/etc/systemd/system/$unit"
        [[ ! -L "$unit_file" ]] || { fail "$unit 是符号链接，拒绝覆盖或操作。"; return 1; }
        if [[ -e "$unit_file" ]]; then
            grep -Fqx -- "# futures.sh project-root: $FUTURES_ROOT" "$unit_file" || {
                fail "$unit 不属于当前项目路径，拒绝操作。"; return 1;
            }
        fi
        if unit_exists "$unit"; then
            fragment="$(systemctl show "$unit" -p FragmentPath --value 2>/dev/null)" || return 1
            [[ "$fragment" == "$unit_file" && -f "$unit_file" ]] || {
                fail "已存在其他来源的 $unit，拒绝接管。"; return 1;
            }
        fi
    done
}

capture_active() {
    ACTIVE_BEFORE=()
    local unit
    for unit in "${UNITS[@]}"; do
        if systemctl is-active --quiet "$unit"; then
            ACTIVE_BEFORE+=("$unit")
        fi
    done
}

stop_units() {
    check_unit_ownership || return 1
    local unit failed=0 state
    for unit in "${UNITS[@]}"; do
        if unit_exists "$unit"; then
            systemctl stop "$unit" || failed=1
            state="$(systemctl is-active "$unit" 2>/dev/null || true)"
            case "$state" in active|activating|deactivating) failed=1 ;; esac
        fi
    done
    [[ "$failed" == 0 ]] || { fail '服务未完全停止，已取消后续覆盖操作。'; return 1; }
}

resume_previous() {
    local unit failed=0 scope="worker"
    for unit in "${ACTIVE_BEFORE[@]}"; do
        systemctl start "$unit" || failed=1
    done
    RECOVERY_PENDING=0
    if [[ "$failed" == 0 && "${#ACTIVE_BEFORE[@]}" -gt 0 ]]; then
        if [[ "${#ACTIVE_BEFORE[@]}" == 2 ]]; then scope="all"
        elif [[ "${ACTIVE_BEFORE[0]}" == futures-web.service ]]; then scope="web"
        fi
        admin ready --scope "$scope" --timeout 10 || failed=1
    fi
    if [[ "$failed" != 0 ]]; then
        fail '原服务自动拉起失败，请使用菜单 4/5 查看状态和日志。'
        return 1
    fi
}

rollback_code() {
    [[ "$CODE_CHANGED" == 1 && -n "$CODE_RESCUE" && -f "$CODE_RESCUE/manifest.json" ]] || return 0
    say '安装未完成，恢复本次替换前的程序文件。'
    "$SYSTEM_PYTHON" - "$FUTURES_ROOT" "$CODE_RESCUE" <<'PY'
import json, os, shutil, sys, tempfile
from pathlib import Path
root, rescue = map(Path, sys.argv[1:])
for item in json.loads((rescue/'manifest.json').read_text()):
    destination = root/item['name']
    if item['existed']:
        fd, temporary = tempfile.mkstemp(prefix='.futures-rollback-', dir=destination.parent)
        os.close(fd)
        try:
            shutil.copy2(rescue/'old'/item['name'], temporary)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
    elif destination.exists():
        destination.unlink()
PY
    local result=$?
    CODE_CHANGED=0
    return "$result"
}

cleanup() {
    local original=$? directory
    if [[ "$CODE_CHANGED" == 1 || "$PROXY_CHANGED" == 1 ]]; then
        stop_units || true
        restore_proxy || true
        rollback_code || true
    fi
    if [[ "$RECOVERY_PENDING" == 1 ]]; then
        resume_previous || true
    fi
    for directory in "${TEMP_DIRS[@]}"; do
        case "$directory" in /tmp/futures-manager.*) rm -rf -- "$directory" ;; esac
    done
    return "$original"
}

abort_install() {
    # A multi-unit start can partially succeed. Stop those processes before
    # replacing their program files, then resume only the previously active set.
    stop_units || true
    restore_proxy || true
    rollback_code || true
    local unit
    for unit in "${NEW_UNITS[@]}"; do
        systemctl disable "$unit" >/dev/null 2>&1 || true
        if [[ -f "/etc/systemd/system/$unit" ]] && grep -Fqx -- "# futures.sh project-root: $FUTURES_ROOT" "/etc/systemd/system/$unit"; then
            rm -f -- "/etc/systemd/system/$unit" || true
        fi
    done
    systemctl daemon-reload || true
    resume_previous || true
}

select_python() {
    local candidate
    for candidate in python3 python3.13 python3.12 python3.11 python3.10; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys, sqlite3, ssl; raise SystemExit(sys.version_info < (3,10))' >/dev/null 2>&1; then
            SYSTEM_PYTHON="$(command -v "$candidate")"
            return 0
        fi
    done
    return 1
}

install_dependencies() {
    require_debian || return 1
    local missing=() minor
    if ! select_python; then
        missing+=(python3 python3-venv)
    elif ! "$SYSTEM_PYTHON" -c 'import venv, ensurepip' >/dev/null 2>&1; then
        minor="$("$SYSTEM_PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" || return 1
        missing+=("python${minor}-venv")
    fi
    [[ -s /etc/ssl/certs/ca-certificates.crt ]] || missing+=(ca-certificates)
    command -v ss >/dev/null 2>&1 || missing+=(iproute2)
    if command -v nginx >/dev/null 2>&1 && ! command -v openssl >/dev/null 2>&1; then missing+=(openssl); fi
    if [[ "${#missing[@]}" -gt 0 ]]; then
        say "安装缺失依赖：${missing[*]}"
        apt-get update || return 1
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}" || return 1
    fi
    select_python || { fail '系统 Python 低于 3.10。请使用 Debian 12+、Ubuntu 22.04+ 或先安装 Python 3.10+。'; return 1; }
    "$SYSTEM_PYTHON" -c 'import venv, ensurepip' >/dev/null 2>&1 || { fail '缺少所选 Python 对应的 venv/ensurepip。'; return 1; }
    if [[ ! -x "$PYTHON" ]]; then
        "$SYSTEM_PYTHON" -m venv "$VENV" || return 1
    fi
    "$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3,10))' || {
        fail '已存在的虚拟环境低于 Python 3.10，请保留 data/backup 后手动重建 .venv。'; return 1;
    }
    # Only these three dependencies belong to this application; no global pip upgrade.
    "$SYSTEM_PYTHON" - "$SOURCE_DIR/requirements.txt" <<'PY'
from pathlib import Path
import re, sys
names=set()
for raw in Path(sys.argv[1]).read_text().splitlines():
    line=raw.strip()
    if not line or line.startswith('#'): continue
    match=re.fullmatch(r'(Flask|gunicorn|tzdata)(?:[<>=!~0-9.,]+)?', line, re.I)
    if not match: raise SystemExit('依赖文件含非本项目依赖，已取消安装')
    names.add(match.group(1).lower())
if names != {'flask','gunicorn','tzdata'}: raise SystemExit('依赖文件缺少 Flask/gunicorn/tzdata')
PY
}

select_source() {
    if [[ -f "$SCRIPT_DIR/futures/app.py" ]]; then
        SOURCE_DIR="$SCRIPT_DIR/futures"
    elif [[ -f "$SCRIPT_DIR/app.py" && -f "$SCRIPT_DIR/manage.py" ]]; then
        SOURCE_DIR="$SCRIPT_DIR"
    elif [[ -f "$FUTURES_ROOT/app.py" && -f "$FUTURES_ROOT/manage.py" ]]; then
        SOURCE_DIR="$FUTURES_ROOT"
    else
        fail "未找到完整本地源码。请将 futures.sh 与 futures/ 文件夹放在一起，或先复制源码到 $FUTURES_ROOT。"
        return 1
    fi
    SOURCE_DIR="$(realpath -- "$SOURCE_DIR")" || return 1
    local relative directory
    for relative in "${CODE_FILES[@]}"; do
        [[ -f "$SOURCE_DIR/$relative" && ! -L "$SOURCE_DIR/$relative" ]] || { fail "完整程序缺少普通文件：$relative"; return 1; }
        directory="$(dirname -- "$SOURCE_DIR/$relative")"
        while [[ "$directory" != "$SOURCE_DIR" && "$directory" != / ]]; do
            [[ ! -L "$directory" ]] || { fail "源码目录不能是符号链接：$directory"; return 1; }
            directory="$(dirname -- "$directory")"
        done
    done
    say "本地程序来源：$SOURCE_DIR"
}

deploy_code() {
    if [[ "$SOURCE_DIR" == "$FUTURES_ROOT" ]]; then
        say '当前程序位于安装目录，保留原代码，仅更新依赖和服务。'
        return 0
    fi
    new_temp CODE_RESCUE || return 1
    "$SYSTEM_PYTHON" - "$SOURCE_DIR" "$FUTURES_ROOT" "$CODE_RESCUE" "${CODE_FILES[@]}" <<'PY'
import json, os, shutil, sys, tempfile
from pathlib import Path
source, root, rescue = map(Path, sys.argv[1:4])
names=sys.argv[4:]
manifest=[]
for name in names:
    origin, target = source/name, root/name
    for component in (target, *target.parents):
        if component.is_symlink(): raise SystemExit('目标路径不能包含符号链接：'+str(component))
        if component == root: break
    if target.exists() and not target.is_file(): raise SystemExit('程序路径被其他类型文件占用：'+name)
    existed=target.is_file()
    manifest.append({'name':name,'existed':existed})
    if existed:
        previous=rescue/'old'/name
        previous.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, previous)
(rescue/'manifest.json').write_text(json.dumps(manifest))
PY
    [[ $? == 0 ]] || return 1
    CODE_CHANGED=1
    "$SYSTEM_PYTHON" - "$SOURCE_DIR" "$FUTURES_ROOT" "${CODE_FILES[@]}" <<'PY'
import os, shutil, sys, tempfile
from pathlib import Path
source, root = map(Path, sys.argv[1:3])
for name in sys.argv[3:]:
    destination=root/name
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary=tempfile.mkstemp(prefix='.futures-code-', dir=destination.parent)
    os.close(fd)
    try:
        shutil.copyfile(source/name, temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
PY
}

write_units() {
    check_unit_ownership || return 1
    local temp unit kind
    new_temp temp || return 1
    for kind in web worker; do
        admin unit --kind "$kind" --prepare-paths > "$temp/futures-$kind.service" || return 1
    done
    for unit in "${UNITS[@]}"; do
        install -m 0644 -- "$temp/$unit" "/etc/systemd/system/$unit" || return 1
    done
    systemctl daemon-reload
}

install_manager() {
    local temporary destination="$FUTURES_ROOT/futures.sh"
    if [[ "$SCRIPT_PATH" != "$destination" ]]; then
        [[ ! -L "$destination" ]] || { fail '管理脚本目标不能是符号链接。'; return 1; }
        temporary="$(mktemp "$FUTURES_ROOT/.futures-manager.XXXXXX")" || return 1
        cp -- "$SCRIPT_PATH" "$temporary" && chmod 0700 "$temporary" && mv -f -- "$temporary" "$destination" || {
            rm -f -- "$temporary"; return 1;
        }
    fi
}

install_project() {
    require_root && require_systemd && require_debian && check_unit_ownership && select_source || return 1
    local marker="$FUTURES_ROOT/.futures-owned" password="" port="8197" result created configured unit domain cert key proxy_info default_domain
    NEW_UNITS=()
    for unit in "${UNITS[@]}"; do
        [[ -f "/etc/systemd/system/$unit" ]] || NEW_UNITS+=("$unit")
    done
    if [[ -e "$marker" ]]; then
        [[ "$(cat -- "$marker")" == "futures|$FUTURES_ROOT" ]] || { fail '项目归属标记与当前路径不一致。'; return 1; }
    elif [[ -e "$FUTURES_ROOT" && "$SOURCE_DIR" != "$FUTURES_ROOT" ]]; then
        local entry
        for entry in "$FUTURES_ROOT"/* "$FUTURES_ROOT"/.[!.]*; do
            [[ ! -e "$entry" ]] || { fail '目标目录非空且无期货归属标记，请换用专用空目录。'; return 1; }
        done
    fi
    mkdir -p -- "$FUTURES_ROOT" "$FUTURES_ROOT/data" "$FUTURES_ROOT/backup" || return 1
    chmod 0700 "$FUTURES_ROOT" "$FUTURES_ROOT/data" "$FUTURES_ROOT/backup" || return 1
    printf '%s\n' "futures|$FUTURES_ROOT" > "$marker" || return 1
    install_dependencies || return 1
    capture_active
    RECOVERY_PENDING=1
    if ! stop_units; then resume_previous || true; return 1; fi
    say '安装期货虚拟环境依赖（Flask、gunicorn、tzdata）。'
    if ! "$PYTHON" -m pip install --disable-pip-version-check -r "$SOURCE_DIR/requirements.txt"; then
        fail '依赖安装失败，已保留运行数据和原程序。'
        resume_previous || true
        return 1
    fi
    if ! deploy_code; then abort_install; return 1; fi
    configured="$(admin config --field auth.configured 2>/dev/null || true)"
    if [[ "$configured" != true ]]; then
        prompt_secret password '首次登录密码（至少 6 位；回车随机生成 6 位）' || { abort_install; return 1; }
        prompt port '网页外部 TCP 端口' 8197 || { abort_install; return 1; }
        result="$(printf '%s\n' "$password" | admin init --port "$port" --password-stdin --check-port)"
    else
        result="$(admin init </dev/null)"
        port="$(admin config --field web.port)" || { abort_install; return 1; }
    fi
    local init_status=$?
    unset password
    if [[ "$init_status" != 0 ]]; then abort_install; return 1; fi
    printf '%s\n' "$result"
    created="$(printf '%s\n' "$result" | "$PYTHON" -c 'import json,sys; print("yes" if json.load(sys.stdin).get("created") else "no")')" || { abort_install; return 1; }
    default_domain="$(existing_domain)" || default_domain='dism.eu.org'
    ask_domain_cert domain cert key "$default_domain" || { abort_install; return 1; }
    capture_proxy || { abort_install; return 1; }
    proxy_info="$(proxy_cli configure --domain "$domain" --port "$port" --cert "$cert" --key "$key")" || { abort_install; return 1; }
    printf '%s\n' "$proxy_info" | "$PYTHON" -c 'import json,sys; data=json.load(sys.stdin); print("访问地址："+data["url"]); print("HTTPS 状态："+data["detail"])' || { abort_install; return 1; }
    # Check real GET /backtest rendering against the preserved runtime DB before
    # replacing an existing working service. This does not fetch remote quotes.
    if ! "$PYTHON" "$FUTURES_ROOT/check_backtest.py" --root "$FUTURES_ROOT"; then
        fail '回测页面渲染检查未通过，正在恢复原程序。请查看上方堆栈日志。'
        abort_install
        return 1
    fi
    if ! write_units || ! install_manager; then abort_install; return 1; fi
    systemctl enable "${UNITS[@]}" || { abort_install; return 1; }
    if ! systemctl start "${UNITS[@]}"; then
        fail '服务启动失败，运行数据已保留；请查看日志。'
        abort_install
        return 1
    fi
    if ! admin ready --scope all --timeout 15 || ! probe_public; then
        fail '安装后的服务或代理健康检查未通过，正在恢复之前的程序及运行状态。'
        abort_install
        return 1
    fi
    CODE_CHANGED=0
    PROXY_CHANGED=0
    RECOVERY_PENDING=0
    firewall_cli open --port "$port" || say '警告：防火墙自动放行失败；其他服务的规则未被修改。'
    say '安装/更新完成，网页与行情服务已启动且启用开机启动。'
    say 'Nginx 仅使用 Futures 专属 vhost，不会覆盖其他站点或修改 443 回落。'
    say "管理脚本：bash $FUTURES_ROOT/futures.sh"
    if [[ "$created" == yes ]]; then
        say '请保存以下首次登录信息（仅需密码）：'
        cat -- "$FUTURES_ROOT/initial_credentials.txt"
        say "凭据文件：$FUTURES_ROOT/initial_credentials.txt（权限 600）。"
    fi
    show_status
}


restart_all() {
    require_root && require_systemd && need_runtime && check_unit_ownership || return 1
    ui_title '重启全部服务'
    ui_text "$UI_CYAN" '重启网页服务和行情服务，检查两者的真实运行状态。'
    if ! write_units || ! systemctl restart "${UNITS[@]}"; then
        ui_error '服务重启失败，请查看当前运行状态和系统日志。'
        show_status || true
        return 1
    fi
    if ! admin ready --scope all --timeout 15; then
        ui_error '服务尚未通过网页及行情就绪检查。'
        show_status || true
        return 1
    fi
    ui_success '期货网页与行情服务已全部重启，且通过就绪检查！'
    show_status
}

show_status() {
    need_runtime || return 1
    local unit active pid port color label
    ui_title '当前运行状态'
    if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
        for unit in "${UNITS[@]}"; do
            active="$(systemctl is-active "$unit" 2>/dev/null || true)"
            pid="$(systemctl show "$unit" -p MainPID --value 2>/dev/null || true)"
            if [[ "$active" == active ]]; then
                color="$UI_GREEN"; label='运行中（active）'
            elif [[ "$active" == failed ]]; then
                color="$UI_RED"; label='异常（failed）'
            else
                color="$UI_YELLOW"; label="未运行（${active:-未知}）"
            fi
            printf '  %s：%s%s%s；进程：%s\n' "$unit" "$color" "$label" "$UI_RESET" "${pid:-无}"
        done
    fi
    admin status || return 1
    port="$(admin config --field web.port)" || return 1
    ui_text "$UI_BLUE" '对外访问：'
    proxy_cli status | "$PYTHON" -c 'import sys,json; d=json.load(sys.stdin); print(("https" if d.get("https") else "http") + "://" + (d.get("domain") or "服务器IP") + ":" + str(d.get("port", 8197)) + "/" if d.get("enabled") else "直接 HTTP：请访问服务器IP上的配置端口")' || true
    ui_text "$UI_BLUE" '网页端口监听：'
    if command -v ss >/dev/null 2>&1; then
        ss -lntp "sport = :$port" || true
    else
        say '系统未安装 ss（iproute2），无法列出监听进程。'
    fi
}

logs_menu() {
    require_root && require_systemd && check_unit_ownership || return 1
    local choice
    while true; do
        ui_title '日志管理'
        ui_item 1 "$UI_CYAN" '网页日志'
        ui_item 2 "$UI_CYAN" '行情日志'
        ui_item 3 "$UI_YELLOW" '实时日志（Ctrl+C 返回）'
        ui_item 0 "$UI_DIM" '返回'
        prompt choice '请选择' 0 || return 1
        case "$choice" in
            1) journalctl -u futures-web.service -n 100 --no-pager ;;
            2) journalctl -u futures-worker.service -n 100 --no-pager ;;
            3)
                trap ':' INT
                journalctl -u futures-web.service -u futures-worker.service -n 50 -f || true
                trap 'exit 130' INT
                ;;
            0) return 0 ;;
            *) say '请输入 0–3。' ;;
        esac
    done
}

backup_now() {
    require_root && need_runtime || return 1
    say '正在生成一致性数据库备份，并按系统设置轮转。'
    backup_cli backup
}

restore_menu() {
    require_root && require_systemd && need_runtime && check_unit_ownership || return 1
    local listing source="" selected="" status=0 deployed_port proxy_was_enabled
    deployed_port="$(admin config --field web.port)" || return 1
    proxy_was_enabled="$(proxy_cli status | "$PYTHON" -c 'import json,sys; print("yes" if json.load(sys.stdin).get("enabled") else "no")')" || return 1
    listing="$(backup_cli list)" || return 1
    ui_title '重建运行数据'
    printf '%s\n' "$listing" | "$PYTHON" -c '
import json,sys
items=json.load(sys.stdin)[:30]
if not items: print("本地暂无备份。")
for number,item in enumerate(items,1): print("{}) {}  {:,} 字节".format(number,item["name"],item["size"]))
' || return 1
    say '只恢复 runtime.sqlite3 中的标的、账户、快照、交易和设置。'
    say "备份目录：$FUTURES_ROOT/backup（新备份按 YYYYMMDD 日期命名）"
    say '直接回车恢复本地最新日期；输入 20261108 恢复该日期；也支持上方编号、旧备份路径或 rclone remote。'
    prompt source '恢复日期 / 来源（回车最新）' latest || return 1
    if [[ "$source" =~ ^[0-9]{8}$ ]]; then
        : # Directly resolve the YYYYMMDD backup (or matching legacy folder).
    elif [[ "$source" =~ ^[0-9]+$ ]]; then
        selected="$(printf '%s\n' "$listing" | "$PYTHON" -c '
import json,sys
items=json.load(sys.stdin)[:30]
index=int(sys.argv[1])-1
if not 0<=index<len(items): raise SystemExit("备份编号无效")
print(items[index]["path"])
' "$source")" || return 1
        source="$selected"
    fi
    say "恢复来源：$source"
    confirm_no '确认覆盖当前期货运行数据？' || { say '已取消重建。'; return 0; }
    capture_active
    RECOVERY_PENDING=1
    if ! stop_units; then resume_previous || true; return 1; fi
    backup_cli restore --services-stopped --confirm --source "$source" || status=$?
    if [[ "$status" != 0 ]]; then
        say '恢复失败，当前数据库未被未校验数据替换；正在重新拉起原先运行的服务。'
    else
        say '运行数据已恢复，程序文件保持当前版本。历史待推送任务不会补发。'
    fi
    # Deployment-specific external port must match the owned Nginx listener;
    # never accidentally bind restored Gunicorn directly onto the proxy port.
    if [[ "$status" == 0 && "$proxy_was_enabled" == yes ]]; then
        admin port --port "$deployed_port" >/dev/null || status=1
        say "已保留当前外部访问端口 $deployed_port（运行数据与策略仍从备份恢复）。"
    fi
    # Restored worker system settings are read again before services resume.
    if ! write_units; then
        say '恢复后的服务配置生成失败，将尝试使用原服务配置拉起；请检查备份路径。'
        status=1
    fi
    resume_previous || status=1
    return "$status"
}

site_settings() {
    require_root && require_systemd && need_runtime && check_unit_ownership || return 1
    local current_port new_port domain cert key default_domain current_unit result new_state status=0
    current_port="$(admin config --field web.port)" || return 1
    default_domain="$(existing_domain)" || default_domain='dism.eu.org'
    prompt new_port '网页外部 TCP 端口' "$current_port" || return 1
    ask_domain_cert domain cert key "$default_domain" || return 1
    capture_active
    RECOVERY_PENDING=1
    stop_units || { resume_previous; return 1; }
    capture_proxy || { resume_previous; return 1; }
    if [[ "$new_port" != "$current_port" ]]; then
        admin port --port "$new_port" --check || status=1
    fi
    if [[ "$status" == 0 ]]; then
        result="$(proxy_cli configure --port "$new_port" --domain "$domain" --cert "$cert" --key "$key")" || status=1
    fi
    if [[ "$status" == 0 ]]; then
        write_units && resume_previous && probe_public || status=1
    fi
    if [[ "$status" != 0 ]]; then
        say '域名/端口配置失败，正在恢复原设置和旧服务。'
        admin port --port "$current_port" >/dev/null || true
        restore_proxy || true
        write_units || true
        resume_previous || true
        return 1
    fi
    PROXY_CHANGED=0
    firewall_cli open --port "$new_port" || say '请核查防火墙 TCP 端口。'
    if [[ "$new_port" != "$current_port" ]]; then
        firewall_cli remove --port "$current_port" || true
    fi
    printf '%s\n' "$result" | "$PYTHON" -c 'import json,sys; data=json.load(sys.stdin); print("当前地址："+data["url"]); print(data["detail"])'
}

account_port_menu() {
    require_root && need_runtime || return 1
    local choice password
    while true; do
        ui_title '密码 / 域名 / 端口'
        ui_item 1 "$UI_GREEN" '修改登录密码'
        ui_item 2 "$UI_CYAN" '修改域名 / HTTPS / 端口'
        ui_item 0 "$UI_DIM" '返回'
        prompt choice '请选择' 0 || return 1
        case "$choice" in
            1)
                prompt_secret password '新登录密码（至少 6 位；回车保持）' || return 1
                printf '%s\n' "$password" | admin account --password-stdin
                unset password
                ;;
            2) site_settings || true ;;
            0) return 0 ;;
            *) say '请输入 0–2。' ;;
        esac
    done
}

backup_menu() {
    require_root && need_runtime || return 1
    local choice previous target result
    while true; do
        ui_title '备份管理'
        ui_item 1 "$UI_GREEN" '立即备份'
        ui_item 2 "$UI_CYAN" '设置默认备份路径'
        ui_item 3 "$UI_YELLOW" '查看备份状态'
        ui_item 4 "$UI_MAGENTA" '清理遗留暂存（24小时以上）'
        ui_item 0 "$UI_DIM" '返回'
        prompt choice '请选择' 0 || return 1
        case "$choice" in
            1) backup_now || true ;;
            2)
                previous="$(admin config --field system.backup_target)" || return 1
                say '示例：/mnt/rclone/futures；填入时不会重复添加 futures 子目录。'
                say '输入 - 可清空外部目录，只保留本地项目内 backup/。'
                prompt target '默认备份路径' "$previous" || return 1
                [[ "$target" != '-' ]] || target=''
                if [[ "$target" == "$previous" ]]; then say '路径未改变。'; continue; fi
                result="$(admin backup-target --path "$target")" || continue
                if ! write_units || ! systemctl restart futures-worker.service || ! admin ready --scope worker --timeout 10; then
                    say '目录权限更新失败，正在恢复原备份设置。'
                    admin backup-target --path "$previous" || true
                    write_units || true
                    systemctl restart futures-worker.service || true
                    continue
                fi
                printf '%s\n' "$result" | "$PYTHON" -c 'import json,sys; d=json.load(sys.stdin); print("已保存："+d["effective_path"])'
                ;;
            3) admin status || true ;;
            4)
                backup_cli cleanup-staging | "$PYTHON" -c '
import json, sys
d = json.load(sys.stdin)
print(d.get("detail") or ("暂存清理完成：已清理 {} 个目录；恢复 {} 个日期备份".format(
    len(d.get("removed", [])), len(d.get("recovered_dates", [])))))
' || true
                ;;
            0) return 0 ;;
            *) say '请输入 0–4。' ;;
        esac
    done
}

quote_test() {
    need_runtime || return 1
    local symbol
    prompt symbol '合约代码（例如 m2709，回车取消）' '' || return 1
    [[ -n "$symbol" ]] || return 0
    admin quote "$symbol"
}

uninstall_project() {
    require_root && require_systemd && need_runtime && check_unit_ownership || return 1
    local unit erase=""
    [[ -f "$FUTURES_ROOT/.futures-owned" && "$(cat -- "$FUTURES_ROOT/.futures-owned")" == "futures|$FUTURES_ROOT" ]] || {
        fail '缺少项目归属标记，拒绝卸载。'; return 1;
    }
    say '将停止并移除期货网页/行情服务；默认保留程序、数据和备份。'
    confirm_no '确认卸载期货服务？' || { say '已取消卸载。'; return 0; }
    stop_units || return 1
    proxy_cli remove || { fail '专用 Nginx 配置未安全卸载，请先修复后重试，其他站点保持原样。'; return 1; }
    firewall_cli remove || say '部分防火墙规则未移除，请人工核对。'
    for unit in "${UNITS[@]}"; do
        if unit_exists "$unit"; then
            systemctl disable "$unit" || return 1
        fi
        if [[ -f "/etc/systemd/system/$unit" ]]; then
            rm -f -- "/etc/systemd/system/$unit" || return 1
        fi
    done
    systemctl daemon-reload || return 1
    systemctl reset-failed "${UNITS[@]}" >/dev/null 2>&1 || true
    say "期货服务已卸载，$FUTURES_ROOT 中的数据和备份仍在。"
    prompt erase "如需永久删除整个 $FUTURES_ROOT，请输入 DELETE；回车保留" '' || return 1
    if [[ "$erase" == DELETE ]]; then
        init_paths && admin can-delete >/dev/null || return 1
        rm -rf --one-file-system -- "$FUTURES_ROOT" || return 1
        say '期货项目目录已删除。'
        EXIT_AFTER_ACTION=1
    else
        say '已保留项目目录，可再次执行菜单 1 安装服务。'
    fi
}

show_menu() {
    ui_title '闲云期货 · 管理中心'
    ui_text "$UI_DIM" "  项目目录：$FUTURES_ROOT"
    ui_item 1  "$UI_GREEN"  '安装 / 更新'
    ui_item 2  "$UI_GREEN"  '重启全部服务'
    ui_item 3  "$UI_YELLOW" '停止服务'
    ui_item 4  "$UI_CYAN"   '查看运行状态'
    ui_item 5  "$UI_CYAN"   '查看运行日志'
    ui_item 6  "$UI_CYAN"   '检查网页 / HTTPS'
    ui_item 7  "$UI_BLUE"   '备份管理'
    ui_item 8  "$UI_MAGENTA" '重建运行数据'
    ui_item 9  "$UI_BLUE"   '密码 / 域名 / 端口'
    ui_item 10 "$UI_CYAN"   '检测行情'
    ui_item 11 "$UI_RED"    '卸载项目'
    ui_item 0  "$UI_DIM"    '退出'
}

main() {
    init_paths || return 1
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    case "${1:-}" in
        --help|-h)
            say "用法：bash futures.sh；自定义路径：FUTURES_HOME=/opt/futures-prod bash futures.sh"
            say '源码需为脚本旁的完整 futures/ 文件夹；只使用本地程序，不下载仓库。'
            return 0 ;;
        --status) show_status; return $? ;;
        --check-path) say "$FUTURES_ROOT"; return 0 ;;
        '') ;;
        *) fail '未知参数；使用 --help 查看说明。'; return 1 ;;
    esac
    local choice
    while true; do
        show_menu
        prompt choice '请选择' 0 || return 0
        case "$choice" in
            1) install_project || true ;;
            2) restart_all || true ;;
            3) require_root && require_systemd && stop_units && say '期货网页与行情服务已停止。' ;;
            4) show_status || true ;;
            5) logs_menu || true ;;
            6) require_root && need_runtime && proxy_cli health && show_status || true ;;
            7) backup_menu || true ;;
            8) restore_menu || true ;;
            9) account_port_menu || true ;;
            10) quote_test || true ;;
            11) uninstall_project || true ;;
            0) return 0 ;;
            *) say '请输入 0–11。' ;;
        esac
        [[ "$EXIT_AFTER_ACTION" == 0 ]] || return 0
    done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
