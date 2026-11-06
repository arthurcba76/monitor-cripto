# Montando um servidor pessoal para o monitor

O monitor precisa de uma máquina **ligada o tempo todo** e com internet. Ele
consome muito pouco: no uso normal, menos de 100 MB de RAM e quase nada de CPU.
Qualquer uma destas opções serve:

| Opção | Bom para | Cuidados |
|---|---|---|
| **PC ou notebook antigo** | Custo zero e controle total. O notebook tem a vantagem da bateria, que funciona como nobreak. | Consumo de energia, ruído, tampa do notebook (veja abaixo). |
| **Raspberry Pi 4/5** (ou similar) | Gasta poucos watts e é silencioso. | Use um bom cartão SD ou, melhor ainda, um SSD por USB. |
| **Máquina virtual em casa** (Proxmox, VirtualBox, Hyper-V, VMware) | Quem já tem um computador ligado o tempo todo. | O hospedeiro precisa ficar ligado. 1 vCPU, 1 GB de RAM e 10 GB de disco bastam. |
| **VPS na nuvem** | Disponibilidade alta, IP fixo. | Custo mensal. **A Binance bloqueia alguns países e regiões** (por exemplo, os EUA para o binance.com). Escolha um datacenter numa região permitida. |

Este guia usa **Ubuntu Server 24.04 LTS**, mas qualquer Debian ou derivado
funciona do mesmo jeito.

---

## 1. Instalar o sistema

### Em máquina própria
1. Baixe a ISO do **Ubuntu Server LTS** em ubuntu.com/download/server.
2. Grave num pendrive com o Rufus, o balenaEtcher ou o Ventoy.
3. Dê boot pelo pendrive e instale. Durante a instalação:
   - Use o disco inteiro. Se quiser, o LVM facilita aumentar o disco depois.
   - Crie o seu usuário administrador.
   - Marque **"Install OpenSSH server"**.
   - Não precisa instalar nenhum "snap" extra.
4. Tire o pendrive e reinicie.

### Em máquina virtual
1. Crie a VM com 1–2 vCPU, 1–2 GB de RAM e 10–20 GB de disco.
2. Use a rede em modo **bridge**, se quiser acessar a VM por outros aparelhos da sua rede. O NAT também funciona, porque o monitor só precisa de saída para a internet.
3. Monte a ISO e siga os mesmos passos da instalação em máquina própria.
4. Configure o hospedeiro para **iniciar a VM automaticamente** quando ele ligar.

### Em VPS
Crie a instância com a imagem Ubuntu 24.04 e cadastre a sua chave SSH no painel
do provedor.

---

## 2. Primeiros ajustes

Entre por SSH (`ssh seu-usuario@IP-DO-SERVIDOR`) e rode:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3-venv git unattended-upgrades ufw

# Atualizações de segurança automáticas
sudo dpkg-reconfigure -plow unattended-upgrades

# Fuso horário e relógio sincronizado. A Binance recusa pedidos assinados
# quando o relógio está errado (erro -1021).
sudo timedatectl set-timezone America/Sao_Paulo     # troque pelo seu fuso
timedatectl                                         # "System clock synchronized: yes"
```

### Acesso por chave SSH (recomendado)

No **seu computador**, não no servidor:
```bash
ssh-keygen -t ed25519
ssh-copy-id seu-usuario@IP-DO-SERVIDOR
```
Confirme que consegue entrar sem senha. Depois, no servidor, desligue o login por
senha:
```bash
echo -e "PasswordAuthentication no\nKbdInteractiveAuthentication no" | \
  sudo tee /etc/ssh/sshd_config.d/10-sem-senha.conf
sudo systemctl restart ssh
```

### Firewall
O monitor não precisa receber nenhuma conexão. Libere só o SSH:
```bash
sudo ufw allow OpenSSH
sudo ufw enable
```

### Acesso de fora de casa (opcional)
Para não abrir portas no roteador, use uma VPN em malha como o **Tailscale**
(gratuito para uso pessoal) ou o WireGuard. Com o Tailscale instalado no
servidor e no celular, você entra por SSH de qualquer lugar pelo IP `100.x.y.z`
da rede privada.

---

## 3. Ajustes de hardware (máquina própria)

**Ligar sozinho depois de queda de energia:** na BIOS/UEFI, procure *"Restore on
AC Power Loss"* (ou *"AC Recovery"*) e escolha **Power On**.

**Notebook com a tampa fechada:** por padrão, ele suspende ao fechar a tampa.
Para evitar:
```bash
sudo sed -i 's/^#\?HandleLidSwitch=.*/HandleLidSwitch=ignore/;s/^#\?HandleLidSwitchExternalPower=.*/HandleLidSwitchExternalPower=ignore/' /etc/systemd/logind.conf
sudo systemctl restart systemd-logind
```

**Nunca suspender:**
```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

**Bateria de notebook sempre na tomada:** se o fabricante tiver a opção de
limitar a carga (60–80%), ative. Ela aumenta muito a vida útil da bateria.

---

## 4. Instalar o monitor

Crie um usuário próprio, sem sudo, para rodar o programa:
```bash
sudo adduser --system --group --home /opt/monitor-cripto cripto
sudo -u cripto git clone https://github.com/SEU-USUARIO/monitor-cripto.git /opt/monitor-cripto/app
cd /opt/monitor-cripto/app
sudo -u cripto python3 -m venv .venv
sudo -u cripto .venv/bin/pip install -r requirements.txt
sudo -u cripto .venv/bin/python monitor_cripto.py --demo
```

Credenciais (veja o README para saber como obter cada uma):
```bash
sudo -u cripto cp .env.exemplo .env
sudo chmod 600 .env
sudo -u cripto nano .env
sudo -u cripto .venv/bin/python monitor_cripto.py --once   # teste real
```

O `--once` deve terminar sem `ERROR` e mandar o "📊 Resumo do monitor" no
Telegram.

---

## 5. Rodar como serviço

```bash
sudo cp systemd/monitor-cripto.service /etc/systemd/system/
# Se usou outro usuário ou outra pasta, ajuste User=, WorkingDirectory=,
# ExecStart= e ReadWritePaths= no arquivo copiado.
sudo systemctl daemon-reload
sudo systemctl enable --now monitor-cripto
systemctl status monitor-cripto        # deve mostrar "active (running)"
```

O serviço reinicia sozinho se o programa cair e sobe junto com a máquina.

Comandos do dia a dia:
```bash
journalctl -u monitor-cripto -f              # acompanhar ao vivo
tail -f /opt/monitor-cripto/app/monitor.log   # histórico de alertas
sudo systemctl restart monitor-cripto        # depois de mudar config.json ou .env
```

### Atualizar para uma versão nova
```bash
cd /opt/monitor-cripto/app
sudo -u cripto git pull
sudo systemctl restart monitor-cripto
```
O `.env`, o `config.json` e o `estado.json` não são tocados, porque ficam fora
do git.

---

## 6. Problemas comuns

| Sintoma | Causa provável |
|---|---|
| `Binance 401 código -2015` | Chave errada ou colada pela metade, IP diferente do cadastrado na restrição, ou chave sem "Enable Reading". |
| O -2015 aparece "do nada" depois de dias funcionando | O **IP da sua internet mudou** (a maioria das conexões residenciais tem IP dinâmico). Atualize a restrição na Binance ou contrate IP fixo. |
| `código -1021` | Relógio do servidor errado. Rode `timedatectl` e confira se está sincronizado. |
| `código -1022` | O `BINANCE_API_SECRET` está errado. |
| `Telegram recusou a mensagem: 404` | Token inválido ou incompleto. |
| `Telegram recusou a mensagem: 400 chat not found` | Chat ID errado, ou você não mandou `/start` para o bot. |
| `Outro programa já lê este bot (409)` | O mesmo token está em uso por outro programa. Crie um bot só para o monitor ou veja [comandos-externos.md](comandos-externos.md). |
| Funciona com `--once`, mas não envia nada com o tempo | O serviço não está rodando: confira com `systemctl status monitor-cripto`. Lembre também que os relatórios seguem o `/frequencia` e o horário do `/agenda` e que os alertas só aparecem quando há um padrão. Use `/status` para receber o estado na hora. |
| Nenhum aviso de ordens | `Futuros desativado` no log quer dizer que a chave não lê Futuros, e o Spot continua normal. Se não houver nenhuma ordem aberta, também não há relatório. |
