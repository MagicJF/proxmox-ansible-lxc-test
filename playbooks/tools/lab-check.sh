#!/bin/bash
# =============================================================================
# lab-check.sh — el MISMO script para las OCHO workstations del lab de 3 sedes.
#
# No se configura ni se le pasa nada: mira su PROPIA dirección, deduce en qué
# sede está y quién es su puerta de enlace, y comprueba que la red se comporta
# como la topología dice. Se ejecuta igual en ws1 que en ws8.
#
# Por cada uno de los otros 15 dispositivos del lab:
#   1 ALCANCE      ¿contesta?  (el último salto del traceroute es el destino)
#   2 PRIMER SALTO ¿el primer salto es MI puerta de enlace declarada?
#   3 CAMINO       ¿los saltos intermedios son los que predice la topología?
#
# 1 y 2 son duros: un fallo ahí es un fallo real. El 3 es un HALLAZGO — si
# difiere, imprime esperado y obtenido para analizarlo, no para darlo por roto.
# =============================================================================
set -u

# --- el mapa del lab: nombre, dirección, sede (0 = red de gestión), tipo -----
# Una sola verdad: de aquí salen a la vez los destinos y la identidad propia.
LAB="ws1 10.11.0.11 1 pc;ws2 10.11.0.12 1 pc;ws3 10.11.0.13 1 pc;ws4 10.11.0.14 1 pc"
LAB="$LAB;ws5 10.12.0.11 2 pc;ws6 10.12.0.12 2 pc;ws7 10.13.0.11 3 pc;ws8 10.13.0.12 3 pc"
LAB="$LAB;sw1 10.10.10.160 0 sw;sw2 10.10.10.161 0 sw;rt1 10.10.10.162 0 rt"
LAB="$LAB;sw3 10.10.10.163 0 sw;rt2 10.10.10.164 0 rt;sw4 10.10.10.165 0 sw"
LAB="$LAB;rt3 10.10.10.166 0 rt;boot 10.10.10.143 0 boot"

# --- la topología: quién es quién y por dónde se cruza -----------------------
GW_1=10.11.0.1; GW_2=10.12.0.1; GW_3=10.13.0.1   # la puerta de cada sede
W_1_2=10.99.1.1; W_2_1=10.99.1.2                  # WAN 1: rt1 <-> rt2
W_1_3=10.99.2.1; W_3_1=10.99.2.2                  # WAN 2: rt1 <-> rt3

gw_of() { case "$1" in 1) echo "$GW_1";; 2) echo "$GW_2";; 3) echo "$GW_3";; esac; }

# esperado <sede mía> <sede destino> -> saltos intermedios que predice la
# topología. El destino NUNCA va aquí: el último salto del traceroute ES el
# destino y se comprueba aparte, como prueba de alcance.
esperado() {
  case "$1-$2" in
    1-0|2-0|3-0) gw_of "$1" ;;                    # a la gestión: sólo mi router
    1-1|2-2|3-3) : ;;                             # mi sede: L2 directo, 0 saltos
    1-2) echo "$GW_1 $W_2_1" ;;
    1-3) echo "$GW_1 $W_3_1" ;;
    2-1) echo "$GW_2 $W_1_2" ;;
    2-3) echo "$GW_2 $W_1_2 $W_3_1" ;;
    3-1) echo "$GW_3 $W_1_3" ;;
    3-2) echo "$GW_3 $W_1_3 $W_2_1" ;;
  esac
}

# Permite cargar sólo las funciones para probarlas:  LABCHECK_LIB=1 . lab-check.sh
[ "${LABCHECK_LIB:-}" = "1" ] && return 0 2>/dev/null

# --- ¿quién soy? ------------------------------------------------------------
MY_IP="${LABCHECK_MY_IP:-$(ip -4 -o addr show dev eth0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)}"
[ -z "$MY_IP" ] && MY_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
ME=$(echo "$LAB"      | tr ';' '\n' | awk -v ip="$MY_IP" '$2==ip {print $1}')
MY_SITE=$(echo "$LAB" | tr ';' '\n' | awk -v ip="$MY_IP" '$2==ip {print $3}')

if [ -z "$ME" ]; then
  echo "!! Mi dirección es '$MY_IP' y no está en el mapa del lab."
  echo "   Este script es para las 8 workstations; ejecútalo dentro de una."
  exit 2
fi

MY_GW=$(gw_of "$MY_SITE")
MY_HOST=$(hostname 2>/dev/null)
DEF_GW=$(ip route show default 2>/dev/null | awk '/^default/ {print $3; exit}')

echo "=== LAB-CHECK - $ME ($MY_IP) - sede $MY_SITE ==="
if [ "$MY_HOST" = "$ME" ]; then
  printf '  %-12s %-14s %s\n' hostname "$MY_HOST" "OK (coincide con la IP)"
else
  printf '  %-12s %-14s %s\n' hostname "$MY_HOST" "OJO: la IP dice que soy $ME"
fi
printf '  %-12s %-14s %s\n' "dirección" "$MY_IP" "la reservada en el mapa"
if [ "$DEF_GW" = "$MY_GW" ]; then
  printf '  %-12s %-14s %s\n' "puerta enf." "$DEF_GW" "OK (la declarada)"
else
  printf '  %-12s %-14s %s\n' "puerta enf." "${DEF_GW:-ninguna}" "OJO: esperaba $MY_GW"
fi
echo

# --- la prueba, destino a destino -------------------------------------------
printf '%-4s %-14s %-4s %-5s %-8s %-9s %-7s %s\n' DISPO DIRECCIÓN SEDE TIPO ALCANCE 1erSALTO CAMINO DETALLE
echo "---------------------------------------------------------------------------------------"

total=0; ok_reach=0; ok_first=0; ok_path=0; exempt=0
anomalias=""; por_tipo=""

while read -r name ip site kind; do
  [ -z "${ip:-}" ] && continue
  [ "$ip" = "$MY_IP" ] && continue                 # yo no me pruebo
  total=$((total + 1))

  exp=$(esperado "$MY_SITE" "$site")
  raw=$(timeout 20 traceroute -n -q 1 -w 1 -m 8 "$ip" 2>/dev/null)
  got=$(echo "$raw" | awk 'NR>1 && $2!="*" && $2!="" {print $2}')
  got_flat=$(echo $got)
  last=$(echo "$got" | tail -1)
  got_int=$(echo "$got" | awk 'NF{for(i=1;i<NF;i++) printf "%s ",$i}')

  # 1 — alcance
  if [ "$last" = "$ip" ]; then reach=OK; ok_reach=$((ok_reach + 1)); else reach=NO; fi

  # 2 — primer salto (sólo tiene sentido si el destino NO es de mi sede)
  if [ "$site" = "$MY_SITE" ]; then
    first="-"; exempt=$((exempt + 1))
  elif [ "$(echo $got | head -1)" = "$MY_GW" ]; then
    first=OK; ok_first=$((ok_first + 1))
  else
    first=NO
  fi

  # 3 — camino
  if [ "$(echo $exp)" = "$(echo $got_int)" ]; then
    path="="; ok_path=$((ok_path + 1)); det="$got_flat"
  else
    path="DIF"; det="esp[$exp] obt[$got_flat]"
  fi

  printf '%-4s %-14s %-4s %-5s %-8s %-9s %-7s %s\n' "$name" "$ip" "$site" "$kind" "$reach" "$first" "$path" "$det"
  if [ "$path" = "DIF" ]; then
    anomalias="${anomalias}${name}(${ip}) esp[${exp}] obt[${got_flat}]"$'\n'
  fi
  por_tipo="${por_tipo}${kind} ${reach}"$'\n'
done < <(echo "$LAB" | tr ';' '\n')

# --- resumen, y un bloque corto para pegar ----------------------------------
exigidos=$((total - exempt))
echo
echo "--- RESUMEN $ME ---"
printf '  alcance      : %d/%d destinos contestan\n' "$ok_reach" "$total"
printf '  primer salto : %d/%d por mi puerta (%d en mi propia sede exentos)\n' "$ok_first" "$exigidos" "$exempt"
printf '  camino       : %d/%d idéntico al predicho\n' "$ok_path" "$total"
for k in pc rt sw boot; do
  case "$k" in pc) lbl="workstations";; rt) lbl="routers     ";; sw) lbl="switches    ";; boot) lbl="bootstrap   ";; esac
  tot=$(printf '%s' "$por_tipo" | awk -v k="$k" '$1==k' | wc -l)
  oks=$(printf '%s' "$por_tipo" | awk -v k="$k" '$1==k && $2=="OK"' | wc -l)
  [ "$tot" -gt 0 ] && printf '    %s : %d/%d contestan\n' "$lbl" "$oks" "$tot"
done
if [ -n "$anomalias" ]; then
  echo "  caminos distintos al predicho:"
  printf '%s' "$anomalias" | sed 's/^/    - /'
fi
echo
echo "  NOTA: los switches no llevan ruta de vuelta hacia las sedes (su tabla de"
echo "  rutas sólo tiene redes conectadas), así que RECIBEN el ping pero no pueden"
echo "  contestarlo. Si salen en NO es la topología, no el script: los 3 routers"
echo "  sí contestan porque tienen rutas a las tres sedes."

echo
echo "### PEGA ESTO ###"
echo "ws=$ME ip=$MY_IP sede=$MY_SITE host=$MY_HOST gw=${DEF_GW:-ninguna}(esp $MY_GW)"
echo "alcance=$ok_reach/$total primer_salto=$ok_first/$exigidos camino=$ok_path/$total"
for k in pc rt sw boot; do
  tot=$(printf '%s' "$por_tipo" | awk -v k="$k" '$1==k' | wc -l)
  oks=$(printf '%s' "$por_tipo" | awk -v k="$k" '$1==k && $2=="OK"' | wc -l)
  [ "$tot" -gt 0 ] && echo "$k=$oks/$tot"
done
printf '%s' "$anomalias" | sed 's/^/ANOM /'
echo "### FIN ###"

# El código de salida sólo mira lo que DEBE funcionar: workstations y routers.
malos=$(printf '%s' "$por_tipo" | awk '$1=="pc"||$1=="rt"' | grep -c NO)
[ "$malos" = "0" ] && [ "$ok_first" = "$exigidos" ] && exit 0 || exit 1
