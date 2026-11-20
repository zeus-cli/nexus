# ADR 0022 — Host principal, rede e backup baseline (T7)

- **Status:** aceito como baseline operacional; validação no host alvo pendente
- **Data:** 2026-11-20
- **Escopo:** decisões de host, rede privada, armazenamento e backup baseline do T7
- **Depende de:** ADR 0021 (Execution Gate autoritativo no Rust)
- **Plano:** `docs/NEXUS_T7_EXECUTION_PLAN.md`

## Contexto

O diagrama define o Acer Nitro V15 como host principal; o Dell Inspiron 15 3511
é a estação de desenvolvimento. O ambiente disponível para esta preparação é
Linux Mint 22.3, Core
i3-1115G4, aproximadamente 11 GiB de RAM, gráficos Intel UHD e um NVMe de
238,5 GB. Esse levantamento não é evidência do Acer Nitro V15; o inventário do
host alvo continua pendente.

O repositório já contém as units do daemon e do backup, o script baseline e um
runbook. A implantação desses artefatos, a GPU/Ollama, a rede e o restore ainda
precisam ser validados no host alvo.

## Decisões

1. **Rede privada: Tailscale.** Usar uma tailnet com ACLs restritas aos
   dispositivos do owner. A escolha atende o acesso privado sem exigir porta
   pública ou encaminhamento manual no roteador.
2. **TLS e exposição da API.** Durante o T7, manter a API em loopback
   (`127.0.0.1:8765`) e usar a criptografia da tailnet para administração
   remota. Exposição HTTPS com autenticação por dispositivo pertence ao T9.
   Provedores LLM remotos continuam exigindo HTTPS e autorização explícita.
3. **Diretório de dados.** Para o baseline, manter `data/` relativo ao
   repositório e usar `NEXUS_DATA_DIR` quando uma migração for aprovada. Só
   mover dados para `/srv/nexus-data` após confirmar um disco dedicado e testar
   montagem, permissões e recuperação no Acer Nitro V15.
4. **Backup: restic.** Usar repositório em disco externo ou destino de rede,
   fora do repositório Git; senha fora do checkout. Retenção inicial: sete
   snapshots diários e quatro semanais. O restore drill é obrigatório antes de
   fechar o T7.
5. **Caminho da instalação.** Manter `%h/Documentos/nexus`, caminho usado pelas
   units atuais. Uma mudança de instalação requer atualizar e validar todas as
   units.
6. **Orçamento de modelos.** Carregar um modelo por vez. Manter o contexto em
   2048 tokens até medir memória e latência no Acer; só aumentar o orçamento e
   selecionar o catálogo após inventariar GPU/VRAM e executar benchmark.

## Consequências

- `config/nexus.toml` permanece seguro para uso local; o Control Plane Rust
  continua opt-in até a validação operacional do daemon no host alvo.
- `config/backup.env` e a senha do restic são locais e não devem ser adicionados
  ao Git. O exemplo versionado aponta para `/mnt/nexus-backup/restic`.
- O baseline de armazenamento evita mudança de diretórios antes de verificar
  o disco do Acer; o backup fica fora do checkout desde a primeira execução.
- As decisões de rede e recursos estão registradas, mas a instalação e a
  validação não são consideradas concluídas por este ADR.

## Validação pendente

- Inventariar o Acer Nitro V15 e confirmar o disco de dados e a GPU/VRAM.
- Configurar e validar a tailnet, firewall e ausência de portas abertas na LAN.
- Inicializar o repositório restic no destino escolhido e executar restore drill.
- Validar units, autoridade Rust, fail-closed, recuperação de órfãos e soak no
  host real.
