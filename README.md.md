# Гостевая Wi-Fi сеть на MikroTik RouterOS v7.24+

Инструкция для нового WiFi-стека (`wifi-qcom` / `wifi-qcom-ac`, меню `/interface/wifi`).
Для старых устройств на пакете `wireless` команды будут другими — здесь они не рассматриваются.

## Что получится

- Отдельная гостевая точка доступа со своим SSID и паролем.
- Своя подсеть и DHCP, изолированная на L2 (отдельный bridge) и на L3 (firewall).
- Гости не видят друг друга (`client-isolation`).
- Гости не имеют доступа к роутеру (кроме DHCP и DNS) и к основной сети.
- Выход в интернет есть.
- Ограничение скорости **5 Мбит/с на каждое устройство** (отдельно на скачивание и отдачу).
- Не более **10 одновременно подключённых устройств**.
- Общий лимит **10 Мбит/с на всю гостевую сеть**.
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
| Лимит на устройство | `5M` (5 Мбит/с) |
| Максимум устройств | `10` |
| Лимит на всю сеть | `10M` (10 Мбит/с) |
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
    lease-time=1h disabled=no comment="Guest DHCP"
/ip dhcp-server network add address=192.168.77.0/24 gateway=192.168.77.1 dns-server=192.168.77.1
```

Длинный `lease-time` гостям не нужен, час — разумно. DNS-сервером указываем сам роутер (см. шаг 6).

Пул из 10 адресов — это второй рубеж ограничения числа устройств. Так как на `br-guest` включён `arp=reply-only`, устройство без DHCP-lease не получит ARP-ответов и работать не сможет, в том числе со статическим IP. То есть число одновременно работающих устройств жёстко ограничено размером пула. Основной рубеж — `max-clients` на уровне радио (шаг 2).

---

## 2. Виртуальная гостевая точка доступа

Создаём три профиля и виртуальный интерфейс. `datapath.bridge=br-guest` помещает гостевой AP в отдельный bridge, а `client-isolation=yes` изолирует клиентов друг от друга.

```rsc
# Профиль безопасности: WPA2/WPA3, WPS выключен
/interface/wifi/security add name=sec-guest \
    authentication-types=wpa2-psk,wpa3-psk \
    passphrase="ЗАМЕНИТЕ_НА_ПАРОЛЬ" \
    wps=disabled

# Профиль datapath: отдельный bridge + изоляция клиентов
/interface/wifi/datapath add name=dp-guest \
    bridge=br-guest \
    client-isolation=yes

# Профиль конфигурации: SSID, страна, максимум клиентов, привязка профилей
/interface/wifi/configuration add name=conf-guest \
    ssid="Guest-WiFi" \
    country=russia \
    max-clients=10 \
    security=sec-guest \
    datapath=dp-guest

# Виртуальный AP на том же радио, что и основной
/interface/wifi add name=wifi-guest \
    master-interface=wifi1 \
    configuration=conf-guest \
    comment="Guest AP"
```

- `country` укажите своей страны (нижним регистром, например `russia`, `latvia`, `germany`).
- `max-clients=10` — не более 10 одновременно подключённых устройств: 11-й клиент не сможет ассоциироваться с точкой ещё до выдачи IP.
- Пароль сгенерируйте стойкий, например:
  ```bash
  LC_ALL=C tr -dc 'A-HJ-NP-Za-km-z2-9' < /dev/urandom | head -c 20
  ```
  Символы `\ ; , : "` в пароле и SSID допустимы (скрипт QR их корректно экранирует), но проще их не использовать.

Если у вас две радиоточки (2.4 и 5 ГГц) и вы хотите гостевой SSID на обеих — повторите создание интерфейса с `master-interface=wifi2` под другим именем (например, `wifi-guest-2`), используя те же профили.

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

Нужно запретить гостям основную подсеть и общение между собой, разрешить интернет — и при этом **исключить гостевой трафик из FastTrack**, иначе ограничение скорости из шага 7 работать не будет (FastTrack обходит очереди).

Правила добавляются в начало списка (`place-before=0`), каждая следующая команда вставляется выше предыдущей — поэтому в итоге сверху окажутся именно они, в нужном порядке:

```rsc
/ip firewall filter
add chain=forward action=accept out-interface-list=guest \
    comment="guest: internet download (no fasttrack)" place-before=0
add chain=forward action=accept in-interface-list=guest \
    comment="guest: internet upload (no fasttrack)" place-before=0
add chain=forward action=drop in-interface-list=guest dst-address=192.168.77.0/24 \
    comment="guest: drop to guest subnet" place-before=0
add chain=forward action=drop in-interface-list=guest dst-address=192.168.88.0/24 \
    comment="guest: drop to LAN" place-before=0
```

Проверьте результат:

```rsc
/ip firewall filter print where chain=forward
```

Сверху должны быть, по порядку:

1. `guest: drop to LAN`
2. `guest: drop to guest subnet`
3. `guest: internet upload (no fasttrack)`
4. `guest: internet download (no fasttrack)`

…и только затем идут дефолтные правила, включая `fasttrack-connection`.

> Если у вас есть другие локальные подсети — добавьте для них такие же `drop`-правила перед `accept`-правилами.

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

## 7. Ограничение скорости: 5 Мбит/с на устройство и 10 Мбит/с на всю сеть

Ограничение делается через **PCQ** (Per-Connection Queue): трафик группируется по IP-адресу, и каждой группе выдаётся своя полоса. Скачивание группируем по адресу получателя (`dst-address`), отдачу — по адресу отправителя (`src-address`). Чтобы очереди видели гостевой трафик, метим его в mangle.

### 7.1. Метки пакетов (mangle)

```rsc
/ip firewall mangle
add chain=forward action=mark-connection connection-mark=no-mark in-interface-list=guest \
    new-connection-mark=guest-conn comment="guest: mark connection"
add chain=forward action=mark-packet connection-mark=guest-conn in-interface-list=guest \
    new-packet-mark=guest-upload passthrough=no comment="guest: mark upload"
add chain=forward action=mark-packet connection-mark=guest-conn out-interface-list=guest \
    new-packet-mark=guest-download passthrough=no comment="guest: mark download"
```

### 7.2. Типы очередей PCQ

```rsc
/queue type
add name=guest-down kind=pcq pcq-rate=5M pcq-classifier=dst-address
add name=guest-up kind=pcq pcq-rate=5M pcq-classifier=src-address
```

`pcq-rate=5M` — это и есть лимит 5 Мбит/с **на каждый адрес**, то есть на каждое устройство.

### 7.3. Очереди (queue tree)

```rsc
/queue tree
add name=guest-download parent=global packet-mark=guest-download queue=guest-down max-limit=10M comment="Guest 10M total, 5M per device down"
add name=guest-upload parent=global packet-mark=guest-upload queue=guest-up max-limit=10M comment="Guest 10M total, 5M per device up"
```

Получается двухуровневая схема:

- `pcq-rate=5M` — потолок для **одного** устройства;
- `max-limit=10M` — потолок для **всей** гостевой сети;
- при конкуренции PCQ честно делит эти 10 Мбит/с между активными устройствами.

> Итог: одно устройство получит до 5 Мбит/с, а несколько устройств вместе — не более 10 Мбит/с. Это закрывает обход лимита через подмену MAC-адресов: сколько бы «устройств» ни создал злоумышленник, гостевой трафик суммарно не превысит 10 Мбит/с.

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
| Лимит скорости | Speedtest ≈ 5 Мбит/с (или меньше) |
| Лимит устройств | 11-е устройство не подключается к Wi-Fi |
| Лимит на всю сеть | Два активно качающих гостя в сумме дают не более ~10 Мбит/с |

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

/ip dhcp-server remove [find where name=guest-dhcp]
/ip pool remove [find where name=guest-pool]
/ip dhcp-server network remove [find where address=192.168.77.0/24]
/ip address remove [find where address=192.168.77.1/24]

/interface list member remove [find where interface=br-guest]
/interface list remove [find where name=guest]
/interface bridge remove [find where name=br-guest]
```

## Устранение неполадок

| Проблема | Что проверить |
|---|---|
| Гость не получает IP | `br-guest` в списке `guest`; правила `guest: DHCP`; работает ли `/ip dhcp-server print where name=guest-dhcp` |
| Нет интернета | NAT masquerade; правила `guest: internet ...`; `/ip dns print` и `allow-remote-requests` (шаг 6) |
| Гость достучался до LAN | Порядок правил forward (шаг 4.2); не в списке ли `br-guest` в `LAN` |
| Скорость не ограничивается | Гостевой трафик всё ещё в FastTrack — правило `accept` в forward должно идти **выше** `fasttrack-connection`; метки в `/ip firewall mangle` |
| Гость не подключается к Wi-Fi | Достигнут лимит 10 устройств (`/interface/wifi/registration-table print`); пароль ≥ 8 символов для WPA2; `country` задан; AP включён (`/interface/wifi print`) |
| Гость подключён, но без IP | Достигнут размер пула (`/ip dhcp-server lease print where server=guest-dhcp`); правила `guest: DHCP` |
