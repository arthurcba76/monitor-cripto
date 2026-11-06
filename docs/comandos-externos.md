# Comandos quando o bot é compartilhado

O Telegram só permite **um** programa lendo as mensagens de um bot (`getUpdates`).
Se o seu token já é usado por outro programa, como um chatbot, o monitor
detecta o conflito (erro 409), desliga a leitura própria de comandos e continua
enviando alertas normalmente.

A forma mais simples de resolver é criar um bot só para o monitor no @BotFather.

Se preferir manter um bot só, deixe o **outro programa** receber os comandos e
repassá-los ao monitor por uma pasta:

1. No `config.json` do monitor:
   ```json
   { "comandos_telegram": false, "pasta_comandos": "/var/lib/monitor-cripto/comandos" }
   ```
2. Crie a pasta com um grupo compartilhado entre os dois usuários:
   ```bash
   sudo groupadd monitorcmd
   sudo usermod -aG monitorcmd cripto
   sudo usermod -aG monitorcmd USUARIO-DO-OUTRO-PROGRAMA
   sudo install -d -o root -g monitorcmd -m 2770 /var/lib/monitor-cripto/comandos
   ```
   Se os serviços usam `ProtectSystem=strict`, acrescente a pasta ao
   `ReadWritePaths=` e coloque `SupplementaryGroups=monitorcmd` nos dois.
3. No outro programa, ao receber `/status`, `/pausar [prazo]`, `/retomar`, `/tendencia [moeda]`, `/regime`, `/agenda [...]`, `/frequencia [...]` ou `/ajuda [tema]`
   **do seu chat**, grave o texto exato do comando num arquivo novo, de forma
   atômica:

   ```python
   import os, time
   pasta = "/var/lib/monitor-cripto/comandos"
   tmp = f"{pasta}/{time.time_ns()}.tmp"
   with open(tmp, "w") as f:
       f.write(texto_do_comando)          # ex.: "/pausar 2h"
   os.chmod(tmp, 0o660)
   os.rename(tmp, tmp[:-4] + ".cmd")
   ```

O monitor confere a pasta a cada ~3 segundos, apaga o arquivo e responde direto
no Telegram. Se o arquivo ainda existir depois de 20 s, o serviço do monitor
provavelmente está parado.
