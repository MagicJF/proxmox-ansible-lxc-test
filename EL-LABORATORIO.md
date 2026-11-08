# El laboratorio: una empresa de tres sedes

Este repositorio despliega un laboratorio de red que simula **una empresa
pequeña con una central y dos delegaciones**. No es un dibujo cómodo: está
construido para comportarse como hardware de verdad, y todo lo que hay dentro
—máquinas, cables, direcciones, rutas— vive declarado en un solo fichero.

Todo se gobierna **exclusivamente por la API HTTPS de Proxmox**: ningún playbook
entra por SSH en el hipervisor, y ningún router se toca por SSH ni por el puerto
binario de RouterOS. Los siete equipos de red se configuran por su **API REST**.

---

## La topología

```
        SEDE 1 — LA CENTRAL                    SEDE 2                 SEDE 3
   ┌──────────────────────────────────┐   ┌───────────────┐    ┌───────────────┐
   │   ws1     ws2      ws3     ws4   │   │   ws5    ws6  │    │   ws7    ws8  │
   │    │       │        │       │    │   │    │       │  │    │    │       │  │
   │  ether2 ether3   ether4 ether5   │   │ ether2 ether3 │    │ ether2 ether3 │
   │    └───┬───┴────────┴───────┘    │   │    └──┬───┘   │    │    └──┬───┘   │
   │       sw1 ══════════ sw2         │   │      sw3      │    │      sw4      │
   │        │          (troncal)      │   │       │       │    │       │       │
   │        │                         │   │       │       │    │       │       │
   │       rt1 ───────────────────────┼───┼───── rt2      │    │       │       │
   │        │                         │   │               │    │       │       │
   │        └─────────────────────────┼───┼───────────────┼────┼────── rt3     │
   └──────────────────────────────────┘   └───────────────┘    └───────────────┘
                                        ╰── WAN 1 ──╯  ╰───── WAN 2 ─────╯
```

Leído en palabras:

- **En la central** hay dos switches. El primero conecta dos workstations y el
  segundo las otras dos; los dos switches están unidos por un **troncal**, que es
  lo que hace que las cuatro workstations de la sede compartan una sola red. El
  router de la sede es el que sale hacia las otras dos sedes.
- **En cada delegación** hay un switch que conecta sus dos workstations, y un
  router que une ese switch con el resto de routers de la empresa.
- **La central es el núcleo**: las dos delegaciones no se hablan entre sí
  directamente, todo pasa por el router de la central.

---

## Qué es cada pieza

| Máquina | VMID | Qué representa | Dirección de gestión |
|---|---|---|---|
| `sw1` | 160 | Switch de acceso de la central (2 workstations + troncal + router) | `10.10.10.160` |
| `sw2` | 161 | Segundo switch de acceso de la central (2 workstations + troncal) | `10.10.10.161` |
| `rt1` | 162 | Router de la central y núcleo de la WAN | `10.10.10.162` |
| `sw3` | 163 | Switch de la delegación 2 | `10.10.10.163` |
| `rt2` | 164 | Router de la delegación 2 | `10.10.10.164` |
| `sw4` | 165 | Switch de la delegación 3 | `10.10.10.165` |
| `rt3` | 166 | Router de la delegación 3 | `10.10.10.166` |
| `ws1`…`ws4` | 150-153 | Las cuatro workstations de la central | `10.11.0.11` … `.14` |
| `ws5`, `ws6` | 154-155 | Las dos workstations de la delegación 2 | `10.12.0.11`, `.12` |
| `ws7`, `ws8` | 156-157 | Las dos workstations de la delegación 3 | `10.13.0.11`, `.12` |
| `lab-bootstrap` | 143 | Herramienta de despliegue: da a cada router su primera dirección | `10.10.10.53` |

Los siete equipos de red son **RouterOS CHR** (la versión virtual de RouterOS, la
que corre en los equipos de MikroTik). Las ocho workstations son **contenedores
LXC** de Debian.

---

## El direccionamiento

| Qué | Red | Puerta de enlace |
|---|---|---|
| Red de gestión (todos los equipos de red) | `10.10.10.0/24` | `10.10.10.1` (el host) |
| LAN de la central | `10.11.0.0/24` | `10.11.0.1` (rt1) |
| LAN de la delegación 2 | `10.12.0.0/24` | `10.12.0.1` (rt2) |
| LAN de la delegación 3 | `10.13.0.0/24` | `10.13.0.1` (rt3) |
| Enlace WAN central ↔ delegación 2 | `10.99.1.0/30` | rt1 `.1` ↔ rt2 `.2` |
| Enlace WAN central ↔ delegación 3 | `10.99.2.0/30` | rt1 `.1` ↔ rt3 `.2` |

Las direcciones de gestión siguen una regla fácil de recordar: **el último
octeto es el VMID** del equipo (`10.10.10.162` es `rt1`, que es el 162).

Las workstations **no tienen dirección fija**: la reciben por DHCP del router de
su sede, con una reserva atada a su dirección MAC. La reserva no se escribe dos
veces — el router la **deriva** de la declaración de la propia workstation.

---

## Por dónde va un paquete

Este es el dibujo que más ayuda a entender que la red *enruta* de verdad. Una
workstation de la central hablando con una de la delegación 2:

```
   ws1                 rt1                      rt2                ws5
    │                   │                        │                  │
    │  ¿quién es ws5?   │                        │                  │
    │──── 10.11.0.1 ───▶│                        │                  │
    │                   │  no es mía, la conozco por la WAN 1        │
    │                   │──── 10.99.1.2 ────────▶│                  │
    │                   │                        │──── 10.12.0.11 ─▶│
    │                   │                        │                  │
    │◀────────────────────── la respuesta vuelve por donde vino ─────│
```

Y entre las dos delegaciones, el paquete **pasa por la central**, que es lo que
dice la topología:

```
   ws5                 rt2                 rt1                 rt3        ws7
    │                   │                   │                   │          │
    │──── 10.12.0.1 ───▶│                   │                   │          │
    │                   │─── 10.99.1.1 ─────▶│                   │          │
    │                   │                   │─── 10.99.2.2 ─────▶│          │
    │                   │                   │                   │─ 10.13.0.11 ▶│
```

Ese detalle es una señal de que esto es hardware real: **el salto intermedio lo
anuncia la interfaz del router que RECIBE el paquete**, no la del que lo envía.
Por eso entre delegaciones aparece `10.99.1.1` (la punta de rt1 hacia la
delegación 2) y no la punta del propio rt2.

---

## Qué hace que esto se comporte como hardware de verdad

Son cuatro decisiones, y las cuatro se ven en la topología de arriba:

**1. Las workstations son PCs, no contenedores con una pata de gestión.** Cada
una tiene **una sola interfaz** —el cable RJ45— y **ninguna vía de gestión**. No
tienen bloque de configuración: la capa 2 del framework ni siquiera entra en
ellas. Es como se hace en una empresa: un PC tiene una interfaz, toma su
dirección por DHCP, y la gestión de parque es un agente que **sale** hacia fuera,
nunca una conexión entrante.

**2. Cuatro de los siete equipos de red son switches, y lo son por su
configuración, no por su nombre.** Ponen sus patas de datos en un puente de
RouterOS y **no le ponen dirección**. Eso es exactamente lo que distingue un
switch de un router: su dirección vive sólo en su pata de gestión, como la
interfaz de administración de un switch de armario. Un switch con dirección en el
puente empieza a enrutar y la topología deja de ser la del dibujo **sin que nada
falle** — el peor tipo de error, porque no se nota.

**3. Los switches llevan puerta de enlace de gestión.** Sin ella reciben los
pings de su sede y **no pueden contestarlos**: su tabla de rutas sólo conocería
la red de gestión y no sabría volver. Cada switch apunta al router de su sede.

**4. La red se prueba desde dentro, como lo haría un ingeniero.** La verificación
sale de los propios routers (ping, traceroute, tabla de conmutación, concesiones
DHCP), no de una vía de gestión metida en las workstations. Es más barato y más
fiel a la vez.

---

## Las dos capas

El despliegue está partido en dos, y esa separación es la espina dorsal del
repositorio:

```
   CAPA 1 — LA FORMA                          CAPA 2 — LA CONFIGURACIÓN
   (API HTTPS de Proxmox)                     (dentro de cada invitado)

     los 14 bridges (un cable cada uno)         RouterOS ← su API REST HTTP
     las 8 workstations                                · puentes y puertos
     los 7 equipos de red                              · direcciones IP
     lab-bootstrap                                     · servidores DHCP por sede
        │                                               · rutas entre sedes
        │                                               · identidad del equipo
        └── declara la FORMA, nunca la toca ────────┘
```

- **La capa 1** crea las máquinas y los cables. No entra en ninguna.
- **La capa 2** entra en los invitados y los configura. En un RouterOS es
  **siempre** por su API REST, con `ansible.builtin.uri`: nada de SSH, nada del
  puerto binario 8728.

Un mismo fichero, `inventory/group_vars/all.yml`, declara **todo**: qué máquinas
existen, en qué cable está cada una, qué direcciones tienen y cómo se enrutan
entre ellas.

---

## Cómo se despliega

El orden no es un capricho: **es una propiedad real del montaje**. Un cliente
DHCP arranca al crear la máquina, cuando su servidor todavía no existe, y no
reintenta indefinidamente.

```bash
# 1. la forma: bridges, workstations y equipos de red
ansible-playbook playbooks/apply.yml

# 2. la primera dirección de cada router (el problema del huevo y la gallina:
#    sin dirección no hay API, y sin API no se puede configurar)
ansible-playbook configure/site.yml --limit localhost,lab-bootstrap

# 3. reiniciar los routers: su cliente de fábrica ya tiene a quién preguntar

# 4. la configuración de los siete equipos de red, por su API REST
ansible-playbook configure/site.yml

# 5. reiniciar las workstations: ahora sí hay servidor DHCP en su sede
```

En un laboratorio ya montado, el paso 4 basta: un `configure/site.yml` sobre un
estado ya convergido **no cambia nada** (`changed=0`), y esa es la garantía
central del repositorio.

---

## Cómo se verifica

Hay un script, `playbooks/tools/lab-check.sh`, que se ejecuta **dentro de
cualquiera de las ocho workstations** y hace tres comprobaciones contra los otros
quince equipos del laboratorio:

| | Qué comprueba | Dureza |
|---|---|---|
| **alcance** | ¿contesta? | dura |
| **primer salto** | ¿el primer salto es la puerta de enlace que le toca? | dura |
| **camino** | ¿los saltos intermedios son los que predice la topología? | hallazgo |

El mismo fichero sirve para las ocho: mira su propia dirección, deduce en qué
sede está y quién debe ser su puerta de enlace.

**Lo que debe salir** en cualquiera de ellas:

```
alcance=15/15  primer_salto=12/12 (o 14/14 en las delegaciones)  camino=15/15
pc=7/7   rt=3/3   sw=4/4   boot=1/1
```

Es decir: **los quince destinos contestan y el camino es exactamente el que
predice la topología**, en las ocho workstations por igual. Cada una ve a las
otras siete, a los tres routers, a los cuatro switches y al contenedor de
despliegue — y sin una sola anomalía de camino.

---

## Lo que este laboratorio NO es

- **No tiene salida a internet.** El router de la central no tiene ruta por
  defecto hacia fuera: es una red cerrada, como la de un banco. Por eso no se
  puede instalar nada desde dentro de las workstations — y no hace falta: `ping`
  y `traceroute` vienen en la imagen base.
- **`lab-bootstrap` no es un equipo de la empresa simulada.** Es la herramienta
  que trae los switches: existe sólo para dar a cada RouterOS su primera
  dirección, en la única ocasión en que no tiene ninguna. Tiene su propia ruta
  de vuelta hacia las sedes para poder contestar si se le pregunta.
  - **Hace dos trabajos, y por eso no se puede quitar.** El primero es repartir esa
    primera dirección, y para eso **tiene que estar dentro del segmento**: la
    petición de un RouterOS recién nacido es una difusión, y una difusión no cruza
    el enlace del nodo de control, que es punto a punto y enruta. El segundo es ser
    el **camino SSH hacia las sedes**, porque los routers tienen el suyo cerrado a
    propósito y al interior de una sede no se llega sin pasar por aquí.
  - **Y es un contenedor porque es la única forma de colocarlo.** De todos los tipos
    de invitado, sólo la dirección de un contenedor la puede fijar la API al crearlo
    —la de una VM vive dentro de su propio sistema—. No es una máquina especial: es
    la manera que tiene la API de poner algo en esa red.
  - **Para una topología nueva con más equipos no hacen falta más**: reparte por
    reservas derivadas de la declaración, así que cincuenta equipos siguen siendo
    una máquina. Y en cuanto un laboratorio tiene su primer router, el DHCP de
    gestión puede pasar a un router y el contenedor quedar para el salto.

---

## Lo que hace el motor, en una frase

Declaras el estado deseado una vez. Cada verbo —`preflight`, `apply`, `connect`,
`remove`, `configure/site`— compara lo declarado con lo que hay en la plataforma
y aplica **sólo la diferencia**; si no hay diferencia, no toca nada y lo dice.
