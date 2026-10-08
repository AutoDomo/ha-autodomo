# AutoDomo para Home Assistant

Leve seus dispositivos do Home Assistant para o app **AutoDomo** — controle em
tempo real, várias pessoas na mesma casa, sem abrir porta nem expor o HA na
internet. O Home Assistant vira uma *ponte* da sua casa no AutoDomo.

[![Abrir no HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=AutoDomo&repository=ha-autodomo&category=integration)
[![Adicionar integração](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=autodomo)

## Instalar em 3 passos

1. **HACS** → clique no botão *Abrir no HACS* acima (ou *Repositórios
   personalizados* → `AutoDomo/ha-autodomo`, categoria *Integration*) →
   *Baixar* → reinicie o Home Assistant.
2. No **app AutoDomo**, abra a casa → **Pontes → Adicionar Home Assistant**.
   Aparece um código de 8 letras (vale 10 minutos, uso único).
3. No HA, *Configurações → Dispositivos e serviços → Adicionar integração →*
   **AutoDomo** (ou o botão *Adicionar integração* acima) → digite o código →
   confirme a lista do que o app pode ver. Pronto.

Luzes, interruptores, persianas, clima, fechaduras e ventiladores já vêm
marcados; sensores você escolhe. Tudo pode ser mudado depois em *Configurar*.

## Como funciona

- O HA nunca recebe sua senha: o código vira uma identidade própria da ponte,
  presa àquela casa. No app você vê a ponte e pode **revogar** quando quiser —
  o HA perde o acesso na hora e pede um código novo (*Reautenticar*).
- Estado → app a cada mudança (em ~1 s). Comando do app → serviço do HA.
- Presença: o app mostra a ponte online/offline.
- *Reparos*: se uma entidade exposta deixar de existir, o HA avisa.
- *Baixar diagnóstico* na integração gera um relatório sem segredos para suporte.

## O que é exposto

| Domínio HA | No app |
|---|---|
| light | liga/desliga, brilho, cor, temperatura de cor |
| switch, input_boolean | liga/desliga |
| cover | abrir/fechar/parar, posição, inclinação |
| climate | modo, temperatura alvo/atual, ventilação |
| lock | trancar/destrancar |
| fan | liga/desliga, velocidade |
| sensor, binary_sensor | valor e unidade / ligado-desligado |
| scene, button | ativar / pressionar |

Contrato de dados: [`AutoDomo/autodomo-cloud`](https://github.com/AutoDomo/autodomo-cloud)
(`docs/modelo-dados.md`).

## Problemas comuns

- **Código inválido ou expirado** → gere outro no app (vale 10 min, uma vez).
- **Este código é de outra casa** → na reautenticação, gere o código na mesma
  casa da ponte.
- **Entidade não aparece no app** → confira se está marcada em *Configurar*;
  entidades de integrações MQTT entram alguns segundos depois do boot.
- Logs detalhados: `logger: logs: custom_components.autodomo: debug`.

## Desenvolvimento

Sem dependências além do próprio Home Assistant. A seção *Avançado* do
pareamento aponta para o emulador do `autodomo-cloud`;
`tests/emulator_smoke.py` exercita o cliente Firebase fora do HA.

Licença MIT.
