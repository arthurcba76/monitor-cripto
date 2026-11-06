# monitor-cripto

Monitor de carteira de criptomoedas na **Binance** com alertas no **Telegram**.
Ele identifica **suporte e resistência**, avisa sobre **pressão em zonas, faixas
laterais, rompimentos, retestes e falsos rompimentos**, e acompanha as suas
**ordens e posições abertas**, com aviso imediato quando uma ordem é executada.

Foi feito para rodar 24 horas por dia num servidor pessoal (um PC antigo, um
notebook, um Raspberry Pi ou uma máquina virtual) como serviço do `systemd`. É um
único arquivo Python, e a única dependência é `requests`.

> ⚠️ **Não é recomendação de investimento.** O programa não prevê preços e não
> envia ordens. Ele só reconhece padrões técnicos conhecidos e avisa quando eles
> aparecem. Padrões técnicos falham com frequência. Use os alertas como motivo
> para olhar o gráfico, não como ordem de compra ou venda.

---

## Recursos

### Análise de mercado (a cada minuto, só com velas fechadas)

| Alerta | Quando dispara |
|---|---|
| 🔔 **Pressão na resistência** | O preço está perto de uma resistência e testou a zona 2 ou mais vezes nas últimas 12 velas. O programa pesa os sinais a favor do rompimento (volatilidade comprimida, fundos ascendentes, volume crescente, tendência de alta) contra os de rejeição (pavios superiores, RSI acima de 70, divergência de RSI) e dá uma leitura: *rompimento iminente*, *risco de rejeição* ou *indefinido*. |
| 🔔 **Teste de suporte** | A mesma análise no sentido contrário: *perda do suporte iminente*, *suporte segurando* ou *indefinido*. |
| 🔔 **Faixa lateral** | O preço está oscilando entre um suporte e uma resistência há 20 velas ou mais. Mostra o piso, o teto, o meio e em que ponto da faixa o preço está. |
| 🔔 **Rompimento** | A primeira vela que fecha além da zona, vindo do outro lado. Diz se o volume confirmou, qual é a zona provável de reteste e qual é o próximo alvo. |
| 🔔 **Acomodação** | Depois do rompimento, o preço volta à zona e fica do lado novo (a resistência virou suporte, ou o inverso). |
| 🔔 **Falso rompimento** | O preço fecha de volta do lado antigo da zona. |
| 💼 **Variação da carteira** | O valor total da carteira mudou X% desde o último aviso. |
| 📊 **Análise periódica** | Tendência, RSI, suporte e resistência de cada par, na frequência escolhida em `/frequencia`. |

### Ordens e posições (verificadas a cada 15 s)

| Aviso | Quando |
|---|---|
| 🆕 Nova ordem ou posição | Foi aberta uma ordem no Spot ou em Futuros, ou uma posição em Futuros. |
| 🎯 Preço alcançado | O preço chegou ao preço da ordem, mas ela ainda não foi executada. |
| 🟡 Execução parcial | Parte da ordem foi executada. |
| ✅ **Ordem executada** | A ordem foi executada, com o preço médio. |
| ❌ Encerrada | A ordem foi cancelada, expirou ou foi rejeitada. |
| 🏁 Posição encerrada | Uma posição em Futuros foi fechada. |
| 📋 Relatório | A cada 5 minutos, **só enquanto houver algo aberto**: distância do preço até cada ordem, suporte e resistência do par e, em Futuros, entrada, PnL e preço de liquidação. |

### Comandos no Telegram

| Comando | O que faz |
|---|---|
| `/status` | Mostra na hora a análise atual e as ordens ou posições abertas. |
| `/pausar` | Pausa os alertas até você mandar `/retomar`. Aceita prazo: `/pausar 30m`, `/pausar 2h`, `/pausar 1d`. |
| `/retomar` | Volta a enviar os alertas e entrega os avisos de ordens que ficaram guardados durante a pausa. |
| `/tendencia` | Sugestão estatística de **alta, baixa ou lateral**, com o teto e o piso que o preço tende a alcançar antes de romper, a chance estimada de tocar cada lado primeiro, o tempo típico, a faixa provável em 24h e os fatores usados. Outra moeda: `/tendencia ETH`. |
| `/regime` | Termômetro diário do mercado: **EUFORIA, OTIMISMO, NEUTRO, MEDO ou CAPITULAÇÃO**, com MVRV Z-Score, fluxo e saldo de BTC nas corretoras, endereços ativos, Fear & Greed, juros do Fed, dólar e sentimento das notícias. Você também recebe um aviso quando o regime muda. |
| `/config` | Mostra **o que está sendo verificado** (moedas, tempos gráficos, ordens, fontes do regime), o horário geral e o estado de cada categoria, e pergunta se você quer alterar algo. Se sim, um **assistente com botões** guia passo a passo: escolha o item (ex.: 🌡️ Regime) → ligado/desligado → dias da semana → hora de início → hora de fim → frequência → salvar. Cada categoria pode ter o próprio horário ou seguir o horário geral. |
| `/agenda` | Horário de atividade: `/agenda 24/7`, `/agenda uteis 08:00-18:00`, `/agenda seg,qua,sex 09:00-17:00`, `/agenda 22:00-06:00`. Fora do horário nada é enviado, e os avisos ficam guardados até o horário abrir. `/agenda ordens sempre` (padrão) faz as execuções de ordens chegarem na hora, mesmo fora do horário. |
| `/frequencia` | Frequência de cada relatório: `/frequencia analise 30m`, `/frequencia sugestao 1h BTC ETH`, `/frequencia regime 2h`, `/frequencia ordens 5m`, ou `off` para desligar. |
| `/ajuda` | Explica em linguagem simples cada alerta, cada termo dos relatórios (suporte, RSI, ATR, pavio…), os avisos de ordens e os comandos. Para ver só uma parte: `/ajuda alertas`, `/ajuda termos`, `/ajuda ordens`, `/ajuda tendencia`, `/ajuda regime`, `/ajuda agenda` ou `/ajuda comandos`. |

O monitor só aceita comandos vindos do `TELEGRAM_CHAT_ID` configurado e ignora
qualquer outro chat. Durante a pausa ele continua acompanhando tudo: os alertas
de mercado são descartados (só a contagem aparece ao retomar), e os avisos de
ordens ficam guardados.

---

## Início rápido

```bash
git clone https://github.com/SEU-USUARIO/monitor-cripto.git
cd monitor-cripto
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python monitor_cripto.py --demo   # simulação offline, não precisa de chaves

cp .env.exemplo .env && chmod 600 .env      # preencha as chaves (veja abaixo)
cp config.exemplo.json config.json          # opcional: ajuste os parâmetros
.venv/bin/python monitor_cripto.py --once   # um ciclo real e sai
.venv/bin/python monitor_cripto.py          # roda continuamente
```

Para deixar rodando 24 horas por dia, veja **[docs/servidor-pessoal.md](docs/servidor-pessoal.md)**.
Lá estão a preparação do servidor (em máquina própria ou virtual) e a
instalação como serviço.

### 1. Chave da Binance (somente leitura)

1. Na Binance, abra **Perfil → Gerenciamento de API → Criar API** (tipo *gerada pelo sistema*).
2. Marque **apenas "Enable Reading"**. **Nunca** habilite trade nem saque: o programa não precisa disso.
3. Em **restrição de acesso por IP**, informe o IP público do servidor (descubra com `curl -4 ifconfig.me`).
4. Coloque a chave e o segredo em `BINANCE_API_KEY` e `BINANCE_API_SECRET` no `.env`.

Sem chave, o programa ainda funciona, só com os pares de `simbolos_extras`,
porque os preços são públicos.

### 2. Bot do Telegram

1. No Telegram, fale com o **@BotFather**, mande `/newbot` e copie o token para `TELEGRAM_TOKEN`.
2. Abra uma conversa com o seu bot novo e mande `/start`. Sem isso, o bot não consegue te enviar mensagens.
3. Descubra o seu ID com o **@userinfobot** e coloque em `TELEGRAM_CHAT_ID`.

> Use um bot **só para o monitor**. Se o mesmo token já for lido por outro
> programa, como um chatbot, os dois entram em conflito. Nesse caso, veja
> [docs/comandos-externos.md](docs/comandos-externos.md).

**Dica de colagem:** ao colar chaves pelo celular ou por SSH, confira se não
entraram espaços, TABs, quebras de linha ou texto extra. Confira assim, sem
mostrar os valores:

```bash
awk -F= '!/^#/ && NF>1 {print $1, length($2)}' .env   # chaves Binance: 64 caracteres cada
```

---

## Configuração (`config.json`)

Todos os parâmetros têm valor padrão. No `config.json`, coloque só o que quiser
mudar.

| Parâmetro | Padrão | Descrição |
|---|---|---|
| `moeda_cotacao` | `USDT` | Moeda em que os pares são cotados. |
| `usar_carteira` | `true` | Lê os saldos da conta e monitora as moedas que você tem. |
| `simbolos_extras` | `["BTCUSDT"]` | Pares monitorados mesmo sem saldo. |
| `valor_minimo_cotacao` | `10` | Ignora saldos menores que esse valor ("poeira"). |
| `intervalos` | `["1h","4h"]` | Tempos gráficos analisados (`15m`, `1h`, `4h`, `1d`…). |
| `pivo_janela` | `5` | Velas de cada lado para que um topo ou fundo conte como pivô. |
| `min_toques` | `2` | Toques mínimos para que uma zona valha. Use 3 para ver só as zonas mais fortes. |
| `tolerancia_zona_atr` | `0.6` | Pivôs a menos de X ATR uns dos outros viram a mesma zona. |
| `proximidade_atr` | `0.5` | Distância, em ATR, para considerar que o preço está perto da zona. |
| `rompimento_atr` | `0.25` | O fechamento precisa passar X ATR além da zona para contar como rompimento. |
| `volume_rompimento` | `1.3` | O rompimento é confirmado se o volume da vela for pelo menos X vezes a média. |
| `cooldown_min` | `240` | Tempo mínimo antes de repetir o mesmo alerta. |
| `variacao_carteira_pct` | `3.0` | Variação da carteira que dispara o aviso 💼. |
| `agenda_padrao` | `"24/7"` | Horário inicial (depois é ajustado por `/agenda`). |
| `ordens_fora_do_horario` | `true` | As execuções de ordens chegam mesmo fora do horário. |
| `freq_analise_min` | `720` | 📊 Análise periódica, em minutos (0 desliga). |
| `freq_sugestao_min` | `0` | 📈 `/tendencia` automático (0 desliga). |
| `freq_regime_min` | `0` | 🌡️ `/regime` automático (0 = só quando o regime muda). |
| `sugestao_simbolos` | `[]` | Pares da sugestão automática (vazio = `simbolos_extras`). |
| `monitorar_ordens` | `true` | Acompanha as ordens abertas. |
| `monitorar_futuros` | `true` | Inclui Futuros USDⓈ-M. Desliga sozinho se a chave não tiver acesso. |
| `intervalo_ordens_s` | `15` | Frequência da verificação das ordens. |
| `relatorio_ordens_min` | `5` | Intervalo inicial do relatório 📋 enquanto houver algo aberto. |
| `comandos_telegram` | `true` | O próprio monitor lê os comandos enviados ao bot. |
| `pasta_comandos` | `""` | Pasta onde outro programa pode gravar comandos (`*.cmd`). |
| `regime` | `true` | Liga a camada diária (on-chain, sentimento, notícias, macro). |
| `regime_horas` | `6` | Intervalo de recálculo do regime. |
| `regime_peso_tendencia` | `0.20` | Peso do regime dentro do `/tendencia`. |
| `noticias_min` | `30` | Intervalo de leitura e classificação das notícias. |
| `ollama_url` / `ollama_modelo` | `http://127.0.0.1:11434` / `qwen2.5:3b` | Modelo local que classifica as manchetes. |
| `feeds_rss` | 5 portais | Feeds de notícias lidos. |

### Camada de regime (dados diários, todos gratuitos)

Roda numa linha de execução separada (*thread*), então nunca atrasa o
acompanhamento das ordens. Ela recalcula a cada `regime_horas` e lê as notícias a
cada `noticias_min`.

| Item | Fonte (gratuita) | Peso | Leitura |
|---|---|---|---|
| MVRV Z-Score | Coin Metrics Community (`CapMrktCurUSD`, `CapMVRVCur`) | 20% | O *realized cap* sai de market cap ÷ MVRV. Z alto = caro (topo); Z < 0 = barato (fundo). |
| Fluxo líquido de corretoras 7d | Coin Metrics (`FlowInExNtv` − `FlowOutExNtv`) | 15% | Entrada = pressão de venda; saída = acumulação (z-score em 1 ano). |
| Saldo nas corretoras 30d | Coin Metrics (`SplyExNtv`) | 10% | Caindo = menos oferta. |
| Endereços ativos | Coin Metrics (`AdrActCnt`) | 10% | Média de 30 dias contra a de 1 ano. |
| Fear & Greed | Alternative.me | 15% | Leitura contrária. |
| Juros do Fed | FRED `DFEDTARU` | 5% | Cortes em 90 dias = mais liquidez. |
| Dólar | FRED `DTWEXBGS` (substituto gratuito do DXY) | 10% | Correlação inversa. |
| Notícias 24h | Feeds RSS + **modelo local no Ollama** | 15% | Cada manchete classificada como alta, baixa ou neutra. |

O nome do regime vem do MVRV Z-Score e do Fear & Greed. O viés (−1 a +1)
entra no `/tendencia` com peso `regime_peso_tendencia`. O `/tendencia` também
mostra 🚨 **extremos convergentes**: euforia + funding alto + RSI sobrecomprado,
ou o inverso.

**Ollama (opcional):** instale em ollama.com e baixe um modelo pequeno
(`ollama pull qwen2.5:3b`). Sem ele, o item de notícias é ignorado e o resto funciona normalmente.

**Atribuição obrigatória:** os dados da Coin Metrics Community estão sob
**CC BY-NC 4.0** (uso não comercial), e o Fear & Greed exige citar a
Alternative.me. O `/regime` já mostra as fontes.

### Como o `/tendencia` calcula

O comando soma fatores com pesos fixos, num *score* de −1 a +1:

| Fator | Peso | Fonte |
|---|---|---|
| Tendência de cada intervalo (médias 20/50 + inclinação) | 30 | velas |
| Regime diário (viés macro) | 20 | camada de regime |
| Fluxo agressor: % do volume em compras a mercado, últimas 24 velas | 12 | velas (*taker buy volume*) |
| EMAs 9/21/55/200 alinhadas | 10 | velas |
| Livro de ofertas: compra x venda até ±1% do preço | 10 | `/api/v3/depth` |
| RSI (extremos contam contra: risco de correção ou repique) | 10 | velas |
| Open interest em 4h, combinado com a direção do preço | 8 | Futuros (público) |
| Bandas de Bollinger 20/2 (fora da banda = esticado; *squeeze* informado) | 5 | velas |
| Funding rate (extremos = sinal contrário) | 4 | Futuros (público) |
| Proporção de contas long/short (extremos = sinal contrário) | 4 | Futuros (público) |

Os pesos são relativos: o score é normalizado pela soma dos fatores disponíveis.

Os alvos são o teto e o piso mais próximos no primeiro intervalo de `intervalos`.
A chance de tocar cada lado primeiro parte da distância até eles (modelo de passeio
aleatório) e é ajustada pelo score. O tempo típico e a faixa de 24h vêm da
volatilidade das últimas 200 velas. Se o par não tiver Futuros, esses fatores
são ignorados.

> Os pesos são escolhas de projeto e **não foram calibrados com backtest**. Trate
> o resultado como mais um elemento de análise. Contribuições com validação
> histórica são muito bem-vindas.

### Como as zonas são encontradas

1. O programa marca **pivôs**: topos e fundos locais, com 5 velas de cada lado.
2. Pivôs a menos de 0,6 ATR uns dos outros formam uma **zona**, que precisa de 2 toques ou mais.
3. A zona mais próxima acima do preço é a **resistência**, e a mais próxima abaixo é o **suporte**.
4. Só **velas fechadas** entram na análise. O preço ao vivo serve só para medir a proximidade.

O ATR (média do range verdadeiro) faz todas as distâncias se adaptarem à
volatilidade de cada moeda e de cada tempo gráfico.

---

## Arquivos

```
monitor_cripto.py           programa
config.exemplo.json         todos os parâmetros com valores padrão
.env.exemplo                modelo das credenciais
systemd/monitor-cripto.service
docs/servidor-pessoal.md    como montar o servidor e instalar como serviço
docs/comandos-externos.md   comandos quando o bot é compartilhado com outro programa
```

Gerados durante a execução (ficam fora do git): `.env`, `config.json`,
`estado.json` (o que já foi avisado, a pausa e as ordens em acompanhamento), `regime.json` (último regime e manchetes classificadas) e
`monitor.log` (rotativo, até 4 × 5 MB).

## Segurança

- Use chave **somente leitura** e restrita por IP. Se a chave vazar, ninguém consegue operar nem sacar com ela.
- `chmod 600 .env`. Nunca faça commit do `.env`: ele já está no `.gitignore`.
- Não mande prints do `.env` nem do terminal com as chaves visíveis.
- Rode com um usuário próprio, sem sudo (veja o guia do servidor).

## Contribuindo

Issues e pull requests são bem-vindos. Antes de enviar, rode a simulação
offline com `python3 monitor_cripto.py --demo`. Ela deve terminar com
"Acomodação após rompimento de alta".

## Licença

[MIT](LICENSE)
