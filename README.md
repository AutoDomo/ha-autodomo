# ha-autodomo

Integracao do **Home Assistant** com o app **AutoDomo**. Publica os dispositivos
e sensores que voce escolher no app (em tempo real) e executa os comandos que
o app manda. Comunicacao via Firebase Realtime Database do projeto AutoDomo.

Contrato de dados: [`AutoDomo/autodomo-cloud`](https://github.com/AutoDomo/autodomo-cloud)
(`docs/modelo-dados.md`).

## Como funciona

- O Home Assistant e' uma **ponte** da sua casa no AutoDomo. Ele tem identidade
  propria (nao usa sua senha): no app, em *Casa > Pontes > Adicionar*, gere um
  codigo de pareamento e digite aqui. O codigo vale 10 minutos e so' funciona
  uma vez. No app voce pode revogar a ponte a qualquer hora.
- Depois do pareamento, escolha quais entidades expor (luzes, interruptores,
  persianas, sensores, clima, fechaduras, ventiladores, cenas, botoes).
- Estado -> app: a cada mudanca (com debounce de 300 ms).
- App -> Home Assistant: fila de comandos lida por streaming (SSE); cada
  comando vira uma chamada de servico e e' apagado da fila.
- Presenca: `lastSeen` a cada 30 s; o app mostra a ponte online/offline.

## Instalar (HACS)

1. HACS > Integracoes > menu > *Repositorios personalizados* >
   `https://github.com/AutoDomo/ha-autodomo`, categoria *Integration*.
2. Instale **AutoDomo** e reinicie o Home Assistant.
3. Configuracoes > Dispositivos e servicos > *Adicionar integracao* > AutoDomo
   > digite o codigo de pareamento gerado no app.
4. Em *Configurar* (opcoes), marque as entidades que o app deve ver.

## Desenvolvimento

Sem dependencias alem do proprio Home Assistant (`aiohttp` ja' vem).
`tests/emulator_smoke.py` exercita o cliente Firebase contra o emulador do
`autodomo-cloud` (`firebase emulators:start` la', depois
`python tests/emulator_smoke.py`). Opcoes avancadas do fluxo de configuracao
(ative *Modo avancado* no perfil do HA) permitem apontar pro emulador.

## Mapeamento

| Dominio HA | kind | caps | state |
|---|---|---|---|
| light | light | onoff, brightness, color_rgb, color_temp | on, brightness, rgb, color_temp |
| switch, input_boolean | switch | onoff | on |
| cover | cover | open_close, position, tilt | state, position |
| sensor | sensor | unit, device_class | value, unit |
| binary_sensor | binary_sensor | device_class | on |
| climate | climate | modes, target_temp, fan_modes | mode, target_temp, current_temp, fan |
| lock | lock | lock_unlock | locked |
| fan | fan | onoff, percentage | on, percentage |
| scene | scene | activate | - |
| button | button | press | - |

Acoes aceitas em `commands`: `turn_on`, `turn_off`, `toggle`, `set_brightness`,
`set_color`, `set_color_temp`, `open`, `close`, `stop`, `set_position`,
`set_temperature`, `set_mode`, `set_fan_mode`, `lock`, `unlock`,
`set_percentage`, `activate`, `press`.
