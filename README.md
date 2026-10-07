# Гостевая Wi-Fi сеть на MikroTik RouterOS v7.24+

Инструкция для нового WiFi-стека (`wifi-qcom` / `wifi-qcom-ac`, меню `/interface/wifi`).
Для старых устройств на пакете `wireless` команды будут другими — здесь они не рассматриваются.

## Что получится

- Отдельная гостевая точка доступа со своим SSID и паролем.
- Своя подсеть и DHCP, изолированная на L2 (отдельный bridge) и на L3 (firewall).
- Гости не видят друг друга (`client-isolation`).
- Гости не имеют доступа к роутеру (кроме DHCP и DNS) и к основной сети.
- Выход в интернет есть.
- Общий лимит **10 Мбит/с на всю гостевую сеть** (отдельно на скачивание и отдачу), делится между активными устройствами.
- Не более **10 одновременно подключённых устройств**.
- QR-код для печати (генерируется скриптом `wifi_qr.py` из этого репозитория).

## Схема

```
                    ┌──────────────────────────────┐
   Guest-WiFi  ────►│  wifi-guest (виртуальный AP) │
   (гости)          └──────────────┬───────────────┘
                                   │ отдельный bridge br-guest
                                   │ 192.168.77.0/24, DHCP, arp=reply-only
                                   ▼
                        ┌────────────────────┐
   основная LAN  ─────► │  bridge (LAN)      │ 192.168.88.0/24
   (bridge)             └────────┬───────────┘
                                 │
                                 ▼
                              WAN ──► Интернет  (NAT masquerade)
```

Изоляция обеспечивается тремя рубежами:

1. **L2** — гостевой AP подключён к отдельному bridge `br-guest`, между `br-guest` и `bridge` нет форвардинга.
2. **L2 между гостями** — `client-isolation=yes` в datapath.
3. **L3** — правила firewall: гостям запрещена основная подсеть и сама гостевая подсеть, к роутеру разрешены только DHCP и DNS.

## Параметры

Все значения в инструкции можно менять под себя.

| Параметр | Значение по умолчанию |
|---|---|
| Основной bridge (LAN) | `bridge` |
| Основной SSID / радио | `wifi1` (проверить: `/interface/wifi print`) |
| Гостевой bridge | `br-guest` |
| Гостевая подсеть | `192.168.77.0/24`, шлюз `192.168.77.1` |
| Основная подсеть (LAN, для справки) | `192.168.88.0/24` |
| SSID | `Guest-WiFi` |
| Шифрование | WPA2-PSK + WPA3-PSK |
| Максимум устройств | `10` |
| Лимит на всю сеть | `10M` (10 Мбит/с, делится между устройствами) |
| Размер DHCP-пула | 10 адресов |

> **Важно:** основная подсеть указана как `192.168.88.0/24`. Если у вас другая — замените во всех правилах firewall. Если в сети есть другие локальные подсети (IoT, серверы и т.п.), их тоже нужно добавить в запреты.

---

## 0. Подготовка

Сделайте резервную копию и уточните имена объектов:

```rsc
/export file=backup-before-guest

/interface bridge print
/interface/wifi print
/interface list print
```

Убедитесь, что гостевого интерфейса ещё нет и основной bridge называется `bridge`, а рабочее радио — `wifi1` (или `wifi2` для 2.4 ГГц). Ниже подставляйте свои имена.

---

## 1. Гостевая подсеть: bridge, адрес, DHCP

```rsc
# Отдельный bridge. arp=reply-only защищает от статических IP мимо DHCP и от ARP-спуфинга.
/interface bridge add name=br-guest comment="Guest WiFi" arp=reply-only

# Шлюз гостевой подсети
/ip address add address=192.168.77.1/24 interface=br-guest comment="Guest gateway"

# Пул и DHCP-сервер. Диапазон ограничен 10 адресами (см. ниже).
/ip pool add name=guest-pool ranges=192.168.77.10-192.168.77.19
/ip dhcp-server add name=guest-dhcp interface=br-guest address-pool=guest-pool \
    lease-time=1h disabled=no add-arp=yes comment="Guest DHCP"
/ip dhcp-server network add address=192.168.77.0/24 gateway=192.168.77.1 dns-server=192.168.77.1
```

Длинный `lease-time` гостям не нужен, час — разумно. DNS-сервером указываем сам роутер (см. шаг 6).

Пул из 10 адресов — это второй рубеж ограничения числа устройств. Так как на `br-guest` включён `arp=reply-only`, устройство без DHCP-lease не получит ARP-ответов и работать не сможет, в том числе со статическим IP. То есть число одновременно работающих устройств жёстко ограничено размером пула. Основной рубеж — `max-clients` на уровне радио (шаг 2).

> **Обязательно `add-arp=yes` на гостевом DHCP-сервере.** `arp=reply-only` на мосте запрещает динамическое изучение MAC-адресов, поэтому роутер обязан брать MAC клиентов из DHCP-lease. Без `add-arp=yes` гость получит адрес (DHCP — это broadcast, ему ARP не нужен), но роутер не сможет доставить ему обратные пакеты: пропадут ответы DNS, ICMP-эхо и весь download. Симптомы: «интернет не работает» и долгий таймаут, при этом в `/ip dhcp-server lease` lease есть, счётчики `guest: internet` в firewall растут в обе стороны, а `/ip arp print` **пуст для гостевого интерфейса**. Проверка — `ping` до гостя **с роутера**: `timeout`. Это самая частая ошибка в такой схеме.

---

## 2. Виртуальная гостевая точка доступа

Создаём три профиля и виртуальный интерфейс. `datapath.bridge=br-guest` помещает гостевой AP в отдельный bridge, а `client-isolation=yes` изолирует клиентов друг от друга.

```rsc
# Профиль безопасности: WPA2/WPA3, WPS выключен
/interface/wifi/security add name=sec-guest \
    authentication-types=wpa2-psk,wpa3-psk \
    passphrase="ЗАМЕНИТЕ_НА_ПАРОЛЬ" \
    wps=disable

# Профиль datapath: отдельный bridge + изоляция клиентов
/interface/wifi/datapath add name=dp-guest \
    bridge=br-guest \
    client-isolation=yes

# Профиль конфигурации: SSID, страна, максимум клиентов, привязка профилей
/interface/wifi/configuration add name=conf-guest \
    ssid="Guest-WiFi" \
    country=Russia \
    max-clients=10 \
    security=sec-guest \
    datapath=dp-guest

# Виртуальный AP на том же радио, что и основной
/interface/wifi add name=wifi-guest \
    master-interface=wifi1 \
    configuration=conf-guest \
    comment="Guest AP"
```

- `country` укажите своей страны. В RouterOS 7.24 значение пишется **с заглавной буквы** (`Russia`, `Latvia`, `Germany`); `russia` строчными вызовет ошибку. При необходимости проверьте допустимые значения в `/interface/wifi/radio print detail` (`current-country`).
- Свойства-значения проверяйте по факту: в актуальных версиях `wps=disabled` → **`wps=disable`** (иначе syntax error).
- `max-clients=10` — не более 10 одновременно подключённых устройств: 11-й клиент не сможет ассоциироваться с точкой ещё до выдачи IP.
- Пароль сгенерируйте стойкий, например:
  ```bash
  LC_ALL=C tr -dc 'A-HJ-NP-Za-km-z2-9' < /dev/urandom | head -c 20
  ```
  Символы `\ ; , : "` в пароле и SSID допустимы (скрипт QR их корректно экранирует), но проще их не использовать.

Если у вас две радиоточки (2.4 и 5 ГГц) и вы хотите гостевой SSID на обеих — повторите создание интерфейса с `master-interface=wifi2` под другим именем (например, `wifi-guest-2`), используя те же профили.

### Вариант: гость на отдельном радио (например, только 2.4 ГГц), без виртуального AP

Если гостевой SSID должен жить **на самом радиомодуле** (например, отдать под гостя весь диапазон 2.4 ГГц, интерфейс `wifi2`), то `wifi2` выступает гостевой точкой напрямую, а виртуальный AP не создаётся. Настройка та же (профили `sec-guest`, `dp-guest`, `conf-guest`), но интерфейс не добавляется через `/interface/wifi add`, а конфигурируется существующий:

```rsc
/interface wifi set wifi2 \
    disabled=no \
    configuration=conf-guest \
    channel.band=2ghz-ax channel.width=20/40mhz
```

Важные отличия от виртуального AP:

- **`disabled=no` обязателен.** После `reset`/восстановления бэкапа физический `wifi2` бывает выключен (`MBX`), и тогда «сеть пропала».
- **Канал задавайте inline** (`channel.band=2ghz-ax`, `channel.width=20/40mhz`) — как в заводском дефконфиге. Профиль `/interface/wifi/channel` не обязателен; если создаёте его, задавайте `band` и (при желании) `frequency`.
- **Мост — статическим портом**, а не через `datapath.bridge`. Для этого варианта создавайте `dp-guest` **без** `bridge=` (в блоке шага 2 просто опустите `bridge=br-guest`) и добавьте порт вручную:

  ```rsc
  /interface/wifi/datapath add name=dp-guest client-isolation=yes
  /interface bridge port set [find interface=wifi2] bridge=br-guest
  ```

  `datapath.bridge` на мастер-интерфейсе создаёт **динамический** порт в мосту (он бывает в состоянии INACTIVE); статический порт надёжнее.

Основное радио (`wifi1`) при этом продолжает раздавать основную сеть — диапазоны не конфликтуют.

---

## 3. Список интерфейсов `guest`

Отдельный interface list нужен для аккуратных правил firewall. **Не добавляйте `br-guest` в список `LAN`** — иначе гости получат доступ по дефолтным правилам.

```rsc
/interface list add name=guest comment="Guest interfaces"
/interface list member add list=guest interface=br-guest
```

---

## 4. Firewall

### 4.1. Доступ к самому роутеру (chain input)

Разрешаем гостям только DHCP и DNS. Всё остальное к роутеру (Winbox, SSH, API и т.д.) уже отсекается дефолтным правилом `drop all not coming from LAN`. Правила вставляем в начало списка (`place-before=0`).

```rsc
/ip firewall filter
add chain=input action=accept in-interface-list=guest protocol=udp dst-port=67 \
    comment="guest: DHCP" place-before=0
add chain=input action=accept in-interface-list=guest protocol=udp dst-port=53 \
    comment="guest: DNS udp" place-before=0
add chain=input action=accept in-interface-list=guest protocol=tcp dst-port=53 \
    comment="guest: DNS tcp" place-before=0
```

### 4.2. Forward: изоляция и интернет (chain forward)

Нужно запретить гостям локальные подсети (включая основную), разрешить интернет — и **исключить гостевой трафик из FastTrack**, иначе ограничение скорости из шага 7 не заработает (FastTrack обходит очереди).

Сначала соберите список локальных подсетей, куда гостю нельзя. Внесите туда **все** свои локальные сети: основную LAN, гостевую, подсети контейнеров/VLAN, а также подсеть со стороны WAN-аплинка и адреса VPN-туннелей — иначе гость сможет через роутер достучаться до аплинка или внутренних сетей VPN:

```rsc
/ip firewall address-list
add list=guest-blocked address=192.168.88.0/24 comment="main LAN"
add list=guest-blocked address=192.168.77.0/24 comment="guest subnet"
add list=guest-blocked address=192.168.89.0/24 comment="container"
add list=guest-blocked address=192.168.1.0/24 comment="WAN uplink"
add list=guest-blocked address=10.99.0.0/30 comment="vpn transit"
add list=guest-blocked address=10.8.2.5/32 comment="vpn endpoint"
```

затем сами правила:

```rsc
/ip firewall filter
add chain=forward action=drop in-interface-list=guest dst-address-list=guest-blocked \
    comment="guest: drop to local subnets" place-before=0
add chain=forward action=accept in-interface-list=guest out-interface-list=WAN \
    comment="guest: internet upload (no fasttrack)" place-before=0
add chain=forward action=accept out-interface-list=guest \
    comment="guest: internet download (no fasttrack)" place-before=0
```

Про `place-before=0`: правила ложатся в начало списка **блоком в порядке добавления** (а не «каждая следующая выше предыдущей», как иногда пишут). Поэтому добавляйте их ровно в том порядке, в котором они должны идти сверху вниз, и сверяйтесь с выводом.

Условие `out-interface-list=WAN` у upload-правила — страховка: оно сужает «интернет-upload» до выхода в интернет, поэтому даже при перепутанном порядке гость, идущий в локальную сеть, не попадёт под `accept`, а сработает `drop`. Так изоляция не зависит от взаимного порядка двух правил.

Проверьте результат:

```rsc
/ip firewall filter print
```

Сверху должны быть, по порядку:

1. `guest: DHCP`, `guest: DNS udp`, `guest: DNS tcp` (цепочка input)
2. `guest: drop to local subnets`
3. `guest: internet upload (no fasttrack)`
4. `guest: internet download (no fasttrack)`

…и только затем идут дефолтные правила, включая `fasttrack-connection`.

> **Порядок `drop` критичен.** Правило `accept in-interface-list=guest` без ограничения по `out-interface` пропускает *любой* исходящий от гостя трафик, в том числе в LAN. Поэтому либо `drop` идёт раньше, либо `accept` сужается до `out-interface-list=WAN` (как выше).

> Если на роутере уже есть свои forward-правила сверху (например, блокировка рекламы), учтите, что правила гостя вставятся **выше** и перекроют их для гостевого трафика.

---

## 5. NAT

Дефолтная конфигурация уже маскирует весь трафик в WAN, и гостевой подсети это тоже касается. Проверьте:

```rsc
/ip firewall nat print
```

Должно быть правило вида `chain=srcnat action=masquerade out-interface-list=WAN`. Если его нет — добавьте:

```rsc
/ip firewall nat add chain=srcnat action=masquerade out-interface-list=WAN comment="defconf masquerade"
```

---

## 6. DNS

Роутер должен отвечать на DNS-запросы гостей. Убедитесь, что включён кэширующий DNS и заданы upstream-серверы:

```rsc
/ip dns set allow-remote-requests=yes
/ip dns print
```

Если в `servers` пусто, задайте, например:

```rsc
/ip dns set servers=1.1.1.1,8.8.8.8
```

(либо убедитесь, что адреса приходят по DHCP от провайдера).

---

## 7. Ограничение скорости: 10 Мбит/с на всю гостевую сеть

Ограничение делается через очередь в **queue tree** с общим лимитом `max-limit=10M`. Группировку по IP-адресу выполняет **PCQ** (Per-Connection Queue): трафик делится по адресу, поэтому общие 10 Мбит/с честно распределяются между активными устройствами. Скачивание группируем по адресу получателя (`dst-address`), отдачу — по адресу отправителя (`src-address`). Чтобы очереди видели гостевой трафик, метим его в mangle.

### 7.1. Метки пакетов (mangle)

Метим пакеты по направлению относительно гостевого списка интерфейсов — **без** `connection-mark`:

```rsc
/ip firewall mangle
add chain=forward action=mark-packet new-packet-mark=guest-upload passthrough=no \
    in-interface-list=guest comment="guest: mark upload"
add chain=forward action=mark-packet new-packet-mark=guest-download passthrough=no \
    out-interface-list=guest comment="guest: mark download"
```

> Почему без `connection-mark`: если на роутере уже есть VPN, он метит соединения (например, `to_vpn_mark`) и маршрутизирует их в отдельную таблицу. Отдельные гостевые `connection-mark` могут с этим конфликтовать и приводить к тому, что часть трафика пойдёт мимо очередей. Метки **пакетов** по `interface-list` для PCQ вполне достаточно и ни с чем не пересекается.
>
> Если на роутере есть mangle-правила `change-mss` (клэмпинг MSS для VPN), добавляйте гостевые метки **после** них.

### 7.2. Типы очередей PCQ

```rsc
/queue type
add name=guest-down kind=pcq pcq-rate=0 pcq-classifier=dst-address
add name=guest-up kind=pcq pcq-rate=0 pcq-classifier=src-address
```

`pcq-rate=0` означает «без лимита»: PCQ **не** ограничивает отдельный адрес (устройство). Он нужен только как классификатор — разбивает общий поток на подпотоки по IP и делит между ними 10 Мбит/с, заданные в `max-limit` очереди (шаг 7.3). Отдельного потолка на устройство нет: один активный гость может занять все 10 Мбит/с, а при конкуренции несколько устройств делят полосу поровну.

### 7.3. Очереди (queue tree)

```rsc
/queue tree
add name=guest-download parent=global packet-mark=guest-download queue=guest-down max-limit=10M comment="Guest 10M total, shared between devices down"
add name=guest-upload parent=global packet-mark=guest-upload queue=guest-up max-limit=10M comment="Guest 10M total, shared between devices up"
```

> **Как поменять лимит на работающем роутере.** Очереди уже созданы, поэтому не `add`, а `set` (значение — в `max-limit`):
>
> ```rsc
> /queue tree set guest-download max-limit=10M comment="Guest 10M total, shared between devices down"
> /queue tree set guest-upload   max-limit=10M comment="Guest 10M total, shared between devices up"
> ```
>
> Проверить: `/queue tree print`. Изменения применяются сразу, перезагрузка не нужна.

Получается одноуровневая схема:

- `max-limit=10M` — потолок для **всей** гостевой сети;
- `pcq-rate=0` — потолка на отдельное устройство **нет**;
- при конкуренции PCQ честно делит эти 10 Мбит/с между активными устройствами.

> Итог: одно устройство может получить до 10 Мбит/с, а несколько устройств вместе — не более 10 Мбит/с (делят полосу между собой). Это закрывает обход лимита через подмену MAC-адресов: сколько бы «устройств» ни создал злоумышленник, гостевой трафик суммарно не превысит 10 Мбит/с.

> Проверка, что лимит действительно работает, — в шаге 8. Если скорость не ограничивается, значит гостевой трафик всё ещё попадает в FastTrack: перепроверьте порядок правил из 4.2.

---

## 8. Проверка

Подключитесь к `Guest-WiFi` с телефона или ноутбука и проверьте:

| Что проверяем | Ожидаемый результат |
|---|---|
| Получение адреса | `192.168.77.10–19` |
| Интернет | Открывается любой сайт, `ping 1.1.1.1` проходит |
| Доступ к роутеру | `ping 192.168.88.1` — таймаут; `http://192.168.88.1` не открывается |
| Доступ к устройствам LAN | `ping 192.168.88.<устройство>` — таймаут |
| Изоляция гостей | Два гостя не пингуют друг друга |
| Ограничение скорости | Один гость: Speedtest ≈ 10 Мбит/с (или меньше) |
| Лимит устройств | 11-е устройство не подключается к Wi-Fi |
| Лимит на всю сеть | Два активно качающих гостя в сумме дают не более ~10 Мбит/с (делят полосу примерно поровну) |

Полезные команды на роутере:

```rsc
/ip dhcp-server lease print where server=guest-dhcp
/interface/wifi/registration-table print
/queue tree print stats
```

---

## 9. QR-код для печати

В корне репозитория лежит скрипт `wifi_qr.py`: он генерирует PNG с QR-кодом, а под ним печатает название сети и пароль — на случай, если гость не может отсканировать код.

Установка зависимости (один раз):

```bash
pip3 install --user "qrcode[pil]"
```

Генерация:

```bash
python3 wifi_qr.py --ssid "Guest-WiFi" --password "ВАШ_ПАРОЛЬ" -o guest-wifi-qr.png
```

Результат — файл `guest-wifi-qr.png` (300 dpi), готовый к печати. Формат содержимого QR — `WIFI:T:WPA;S:<SSID>;P:<пароль>;;`, он понимается камерой iPhone и Android.

Советы:

- Печатайте QR не мельче 2×2 см и обязательно проверьте сканирование **до** печати тиража.
- Не используйте онлайн-генераторы QR: вы отправите пароль от своей сети третьей стороне. Скрипт `wifi_qr.py` работает полностью локально.
- При смене пароля (см. шаг 10) QR нужно перепечатать.

---

## 10. Ротация пароля

Гостевой пароль стоит периодически менять (раз в несколько месяцев, либо когда круг гостей сменился). Так как в смешанном режиме WPA2/WPA3 атакующий может принудительно перевести клиента на WPA2 и попытаться подобрать пароль офлайн, пароль должен быть длинным и случайным.

```rsc
/interface/wifi/security set sec-guest passphrase="НОВЫЙ_ПАРОЛЬ"
```

После этого сгенерируйте новый QR-код (шаг 9).

---

## Примечания

- **Скрытие SSID** (`hide-ssid=yes`) не является защитой — используйте пароль и WPA2/WPA3.
- **IPv6.** Если у провайдера есть IPv6 и он раздаётся в LAN, гости его не получат (мы не настраивали IPv6 на `br-guest`). Убедитесь, что гостевой трафик не получит доступ к локальным IPv6-адресам: при необходимости добавьте аналогичные правила в `/ipv6 firewall filter`.
- **Не добавляйте `br-guest` в interface list `LAN`.**
- **Флаги wifi-интерфейса.** `/interface/wifi print` показывает: `MBR` — работает и есть клиенты; `MB` — работает, клиентов нет; `MBI` — **inactive, не вещает**; `MBX` — выключен (`disabled=no`, чтобы включить). Флаг `I` на wifi-порте в `/interface bridge port print` при отсутствии клиентов — норма, а не ошибка.
- **Не запускайте `/interface/wifi scan` и `/interface/wifi frequency-scan` через «одноразовый» SSH** (ssh с командой в аргументе). Сканирование «зависает» и держит радио занятым несколько минут, роняя всех клиентов. Для диагностики используйте `monitor`, `registration-table`, `print`.
- **Если радио «залипло»** (SSID не виден никому, при этом `monitor` рапортует `running`, а флаг интерфейса `MBI`): это не лечится перебором настроек. Помогает **заводской сброс** или **холодный старт** (снять и подать питание); обычный `/system reboot` может не помочь. Наблюдалось на `wifi-qcom` (hAP ax³, RouterOS 7.24.x).
- **Сброс и бэкапы.** `/interface/wifi reset <iface>` возвращает интерфейс к дефолту и **выключает** его — не забудьте `disabled=no` и заново задать SSID/канал. При восстановлении бэкапа из командной строки в 7.24 синтаксис `/system backup load name=<файл> password=""` требует явного `password` даже для незапароленного файла.
- **Значения по умолчанию.** После задания профиля (`configuration=` и т.п.) inline-свойства интерфейса имеют **приоритет** над профилем. Снимать свойство следует синтаксисом `!имя` (например `!datapath !channel`), а не `set имя=""` — пустое значение подставляет первый существующий профиль.
- **Несколько точек доступа / CAPsMAN.** В этой инструкции изоляция сделана отдельным bridge. Если гостевой SSID должен раздаваться несколькими AP через провод, вместо `datapath.bridge` используйте VLAN: `datapath.vlan-id=<id>` и VLAN-транк на uplink, по образцу из официальной документации MikroTik (WiFi → CAPsMAN VLAN example).

## Откат

Чтобы полностью удалить гостевую сеть:

```rsc
/interface/wifi remove [find where name~"wifi-guest"]
/interface/wifi/configuration remove [find where name=conf-guest]
/interface/wifi/security remove [find where name=sec-guest]
/interface/wifi/datapath remove [find where name=dp-guest]

/queue tree remove [find where name~"guest-"]
/queue type remove [find where name~"guest-"]
/ip firewall mangle remove [find where comment~"guest:"]
/ip firewall filter remove [find where comment~"guest:"]
/ip firewall address-list remove [find where list=guest-blocked]

/ip dhcp-server remove [find where name=guest-dhcp]
/ip pool remove [find where name=guest-pool]
/ip dhcp-server network remove [find where address=192.168.77.0/24]
/ip address remove [find where address=192.168.77.1/24]

/interface list member remove [find where interface=br-guest]
/interface list remove [find where name=guest]
/interface bridge remove [find where name=br-guest]
```

> Если гость настраивался **на самом радио** (`wifi2` вместо виртуального AP, см. шаг 2), интерфейс `wifi-guest` не создавался — тогда верните `wifi2` к прежнему состоянию:
>
> ```rsc
> /interface bridge port set [find interface=wifi2] bridge=bridge
> /interface wifi set wifi2 !configuration !channel channel.skip-dfs-channels=10min-cac
> /interface wifi set wifi2 disabled=no
> ```

## Устранение неполадок

| Проблема | Что проверить |
|---|---|
| Гость не получает IP | `br-guest` в списке `guest`; правила `guest: DHCP`; работает ли `/ip dhcp-server print where name=guest-dhcp` |
| Гость получил IP, но **интернета нет**, DNS-таймауты | Включён ли `add-arp=yes` на `guest-dhcp` (шаг 1); `/ip arp print` — пуст ли для `br-guest`; `ping` до гостя **с роутера** (должен проходить). Симптом `arp=reply-only` без `add-arp` |
| Нет интернета | NAT masquerade; правила `guest: internet ...`; `/ip dns print` и `allow-remote-requests` (шаг 6) |
| Гость достучался до LAN | Порядок правил forward (шаг 4.2); не в списке ли `br-guest` в `LAN`; есть ли локальная подсеть в `guest-blocked` |
| Скорость не ограничивается | Гостевой трафик всё ещё в FastTrack — правило `accept` в forward должно идти **выше** `fasttrack-connection`; метки в `/ip firewall mangle` |
| Гость не подключается к Wi-Fi | Достигнут лимит 10 устройств (`/interface/wifi/registration-table print`); пароль ≥ 8 символов для WPA2; `country` задан; AP включён (`/interface/wifi print`) |
| **SSID не виден ни одному устройству**, хотя радио `running` | Смотрите флаг интерфейса: `MBI` (inactive) = AP реально не вещает. Конфигом не лечится — помогает **заводской сброс / холодный старт** (перезагрузка по питанию); «мягкий» reboot может не помочь. Проверять видимость **несколькими** клиентами | 
| Гость подключён, но без IP | Достигнут размер пула (`/ip dhcp-server lease print where server=guest-dhcp`); правила `guest: DHCP` |
