# Instruções para agentes de IA

## Objetivo do projeto

Este repositório implementa um framework experimental para avaliar agentes de
engenharia de software baseados em LLMs locais. O benchmark mede o sistema
completo — perfil do modelo, prompt, interface de ferramentas, limites,
repositório e avaliador — em tarefas repository-level. Preserve a
reprodutibilidade e a rastreabilidade dos resultados.

## Antes de alterar

- Leia `README.md` para instalação e comandos disponíveis.
- Para mudanças de arquitetura, tarefas ou avaliação, leia
  `docs/architecture.md` e `docs/methodology.md`.
- Inspecione os testes relevantes e os arquivos `task.yaml` afetados antes de
  mudar runner, agente, ferramentas ou avaliação.
- Não assuma comportamentos não documentados: confirme-os na implementação e
  nos schemas existentes.

## Arquitetura e limites que devem ser preservados

- `local_swe_benchmark/cli.py`: CLI `benchmark` e seleção de tarefas/perfis.
- `agent.py` e `providers/`: loop do agente e integração com o provider. O loop
  deve permanecer desacoplado do provider concreto. O provider LM Studio carrega
  e valida o contexto configurado pela API nativa antes de inferir via API
  OpenAI-compatible; não assuma que `context_length` de perfil é transmitido em
  `/v1/chat/completions`.
- `tools.py`: ferramentas determinísticas e delimitadas ao repositório da
  tentativa.
- `vcs.py` e `runner.py`: checkout isolado, execução, avaliação e coleta de
  artefatos. Cada tentativa deve começar no commit imutável da tarefa em um
  worktree novo; não reutilize workspace de outras tentativas.
- `tasks.py`: descoberta e leitura dos manifests das tarefas. Tarefas novas
  devem ser adicionadas ao dataset sem exigir ramificações específicas por ID
  no código central.
- `reporting.py`: leitura dos registros e geração de relatórios individuais e
  agregados. Preserve dados brutos e relatórios por execução.
- `tasks/`: catálogo por categoria; `repositories/`: fixtures e bundles Git;
  `configs/models.yaml`: perfis experimentais; `results/` e `reports/`: saídas
  locais ignoradas pelo Git.

## Validade experimental

- Mantenha o prompt, schemas e implementação de ferramentas iguais entre os
  modelos comparados. Não codifique nomes de modelos no agente; use perfis em
  `configs/models.yaml`.
- Uma mudança em controles (modelo, quantização, contexto, temperatura, seed,
  limites etc.) define outra configuração experimental.
- O agente não deve receber solução esperada ou conteúdo de hidden tests.
  Hidden tests só podem ser disponibilizados ao avaliador depois que o agente
  parar, em workspace separado.
- Não misture falhas de modelo com falhas de ferramenta, infraestrutura ou
  avaliação. Registre causa, status, saída e métricas disponíveis; telemetria
  ausente deve continuar `null`, nunca ser inventada como zero.
- Não substitua critérios objetivos por avaliação de código “plausível”.
  Tarefas de análise devem usar rubrica determinística e tarefas que alteram
  código devem manter verificações de regressão/aceitação apropriadas.
- Worktrees controlam o estado Git, mas não são sandbox de segurança. Nunca
  disponibilize segredos ao processo do agente. Não rode execuções concorrentes
  contra o mesmo servidor LM Studio, exceto quando isso for variável controlada.

## Alterar ou adicionar tarefas

- Siga o schema versão 1 descrito em `docs/architecture.md`; use IDs únicos,
  categoria válida, versão, commit base imutável e comandos em arrays de
  argumentos (não strings de shell).
- Resolva caminhos relativos conforme o schema e confira os manifests já
  existentes antes de introduzir campos.
- Fixe critérios de aceitação antes de implementar a solução. Verifique a base,
  testes públicos, hidden tests e solução de referência quando aplicável.
- Não coloque patches de referência, respostas de rubric ou dados de hidden
  tests na descrição pública nem em arquivos expostos ao agente.
- Mantenha fixtures e mudanças de tarefa reproduzíveis e documente limitações
  metodológicas relevantes.

## Desenvolvimento

- Requisitos: Python 3.11 ou superior; o runtime principal usa biblioteca
  padrão. PyYAML é opcional para YAML idiomático.
- Instale em modo editável com `python -m pip install -e .` para atualizar o
  comando `benchmark` durante o desenvolvimento.
- A partir da raiz do repositório, a suíte é
  `python -m unittest discover -s tests -v`.
- Depois de alterações em comportamento do framework, execute os testes
  afetados e a suíte completa, quando viável. Não altere testes para esconder
  uma falha de implementação; corrija a causa e preserve cobertura de regressão.
- Para conferir o catálogo e os comandos, use `benchmark tasks list` e
  `benchmark --help` (ou os subcomandos `run --help` e `report --help`).
- Não inclua resultados gerados, worktrees temporários, ambientes virtuais ou
  cache local nos commits (`results/`, `reports/`, `.benchmark_cache/`, `.venv/`
  já são ignorados).

## Relato de mudanças

Ao concluir, resuma o que mudou, os arquivos afetados e quais verificações
foram executadas. Se algo não foi verificado, diga isso claramente. Não afirme
sucesso de modelo/tarefa sem consultar a avaliação objetiva registrada.
