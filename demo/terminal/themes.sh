# Sourced by render.sh. AOX Portfolio UI colours as VHS theme JSON.
theme_json() {
  case "$1" in
    dark) cat <<'J'
{"name":"aox-dark","background":"#0E1012","foreground":"#E6E8EA","cursor":"#3FC1B0","cursorAccent":"#0E1012","selection":"#1C2024","black":"#15181B","brightBlack":"#6B747D","red":"#F2707F","brightRed":"#F58A96","green":"#4CC985","brightGreen":"#6ED79C","yellow":"#E8B23A","brightYellow":"#EEC463","blue":"#7FB2F0","brightBlue":"#9AC4F4","magenta":"#A792F2","brightMagenta":"#BCAAF5","cyan":"#3FC1B0","brightCyan":"#63D0C1","white":"#C3C8CD","brightWhite":"#E6E8EA"}
J
    ;;
    light) cat <<'J'
{"name":"aox-light","background":"#FFFFFF","foreground":"#15181B","cursor":"#0F7A6F","cursorAccent":"#FFFFFF","selection":"#E3F2EF","black":"#15181B","brightBlack":"#5B636B","red":"#B4233A","brightRed":"#B4233A","green":"#1E7A44","brightGreen":"#1E7A44","yellow":"#8A5A00","brightYellow":"#8A5A00","blue":"#1F5FA8","brightBlue":"#1F5FA8","magenta":"#6B4FC2","brightMagenta":"#6B4FC2","cyan":"#0B6159","brightCyan":"#0B6159","white":"#3D444B","brightWhite":"#3D444B"}
J
    ;;
    *) echo "unknown theme: $1 (dark|light)" >&2; return 1 ;;
  esac
}
