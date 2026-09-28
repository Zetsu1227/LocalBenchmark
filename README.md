# Local SWE Benchmark

Framework experimental para comparar LLMs locais como agentes de engenharia de
software. O agente navega por um repositório, usa ferramentas, executa comandos
e testes, e é avaliado por critérios automatizados.

## Requisitos

- Windows com PowerShell (os exemplos abaixo usam PowerShell).
- Python 3.11 ou superior.
- Git disponível no `PATH`.
- LM Studio 0.4.0 ou superior, com o servidor local iniciado e o modelo do perfil
  disponível para executar tarefas com o provider `lmstudio`. A integração usa a
  [API nativa de load](https://lmstudio.ai/docs/developer/rest/load) para aplicar
  o contexto e a [API OpenAI-compatible](https://lmstudio.ai/docs/developer/openai-compat)
  para inferência com ferramentas.

Execute os comandos a partir da pasta raiz do projeto. O CLI resolve caminhos
relativos a partir do diretório atual.

## Instalação

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

O runtime usa somente a biblioteca padrão do Python. Os arquivos de exemplo
estão em sintaxe JSON, válida como YAML. Para ler ou criar arquivos YAML
idiomáticos, instale o suporte opcional:

```powershell
python -m pip install -e ".[yaml]"
```

Se o PowerShell bloquear a ativação do ambiente, use o executável diretamente
(`.\.venv\Scripts\benchmark.exe`) ou ajuste a política de execução da sessão
conforme as regras da sua máquina.

## Configurar o LM Studio e os modelos

1. No LM Studio, disponibilize/baixe o modelo desejado e inicie o servidor local com a API
   OpenAI-compatible disponível (por padrão, `http://localhost:1234/v1`).
2. Edite `configs/models.yaml` e adicione ou ajuste uma entrada sob `models`.
   A chave, por exemplo `qwen3.5-9b-q6`, é o nome de perfil usado no CLI; o campo
   `model` deve corresponder ao modelo servido pelo LM Studio.
3. Registre a quantização, contexto e parâmetros experimentais no perfil. O
   contexto configurado não representa o contexto efetivamente utilizado.

Ao iniciar uma execução, o benchmark consulta os modelos carregados e usa a API
nativa do LM Studio para carregar o modelo do perfil com o `context_length`
definido. Se já houver uma instância daquele mesmo modelo com outro contexto, o
benchmark a descarrega e carrega novamente com o valor do perfil. Outras
instâncias/modelos não são descarregados. A carga só é considerada válida se o
LM Studio confirmar o contexto pedido; a instância, o contexto confirmado e o
tempo de carga aparecem em `run.json` e no report. Essa função requer a API
nativa disponível no LM Studio 0.4.0 ou superior.

O valor do perfil define a capacidade de contexto carregada, não quantos tokens
a conversa consumirá. A conversa pode usar menos tokens; `peak_context` e os
campos relacionados registram o uso observado quando o servidor fornece essa
telemetria.

Perfis de exemplo incluídos: `qwen3.5-9b-q6`, `qwen3.5-9b-q4-k-m`, `qwen3-8b`
e `qwen2.5-coder-7b`. Confirme que o identificador do modelo no perfil
corresponde ao modelo carregado no servidor.

## Comandos do benchmark

### Ajuda

```powershell
benchmark --help
benchmark tasks --help
benchmark run --help
benchmark report --help
```

Se não ativou `.venv`, use `python -m pip install -e .` e chame
`.\.venv\Scripts\benchmark.exe` no lugar de `benchmark`.

### Listar tarefas

```powershell
benchmark tasks list
```

O comando exibe o ID, categoria e arquivo de definição das tarefas encontradas
em `tasks/`. Os IDs atuais incluem:

| ID | Categoria | Objetivo |
| --- | --- | --- |
| `creation-coupon-001` | creation | Implementar o comportamento de cupons no checkout |
| `refactoring-pricing-001` | refactoring | Extrair o cálculo de cada item para um helper, preservando comportamento |
| `analysis-negative-quantity-001` | analysis | Investigar e explicar o cálculo incorreto para quantidades negativas, sem alterar o código |

Use `benchmark tasks list` para confirmar os IDs disponíveis na versão local do
dataset.

### Executar tarefas

Uma execução única:

```powershell
benchmark run --model qwen3.5-9b-q6 --task creation-coupon-001
```

Repetir uma tarefa cinco vezes:

```powershell
benchmark run --model qwen3.5-9b-q6 --task creation-coupon-001 --runs 5
```

Executar todas as tarefas de uma categoria:

```powershell
benchmark run --model qwen3.5-9b-q6 --category refactoring --runs 3
```

Executar todas as tarefas encontradas:

```powershell
benchmark run --model qwen3.5-9b-q6 --runs 1
```

Troque o valor de `--model` por uma chave existente em `configs/models.yaml`.
Para comparar outro modelo ou quantização, configure outro perfil e execute-o
separadamente. Temperatura, `top_p`, seed e contexto são controlados pelo perfil;
uma mudança neles define outra configuração experimental.

Opções disponíveis para `benchmark run`:

| Opção | Descrição |
| --- | --- |
| `--model` | Obrigatória; chave do perfil em `configs/models.yaml` |
| `--task` | Executa somente o ID de tarefa informado |
| `--category` | Filtra por `creation`, `refactoring` ou `analysis` |
| `--runs` | Número de repetições independentes; padrão `1` |
| `--tasks-dir` | Catálogo de tarefas; padrão `tasks` |
| `--models-config` | Arquivo de perfis; padrão `configs/models.yaml` |
| `--results` | Diretório de resultados brutos; padrão `results` |
| `--reports` | Diretório de reports por modelo/tarefa; padrão `reports` |

Cada tentativa recebe um `run_id` próprio e começa no commit fixado da tarefa,
em um Git worktree novo. O agente recebe a descrição da tarefa e as ferramentas,
não uma solução de referência. Não execute várias tarefas concorrentes contra o
mesmo servidor LM Studio, a menos que concorrência faça parte do experimento.

### Reports e exportação

Ao terminar cada tentativa, o comando `run` grava um JSON e um HTML individuais e
atualiza o resumo agregado em:

```text
reports/<perfil-do-modelo>/<task_id>/<run_id>.json
reports/<perfil-do-modelo>/<task_id>/<run_id>.html
reports/<perfil-do-modelo>/<task_id>/summary.json
reports/<perfil-do-modelo>/<task_id>/summary.html
```

Abra o HTML no navegador ou leia o JSON com PowerShell:

```powershell
Invoke-Item .\reports\qwen3.5-9b-q6\creation-coupon-001\summary.html
Get-Content .\reports\qwen3.5-9b-q6\creation-coupon-001\summary.json
```

O resumo agrupa as repetições da mesma tarefa e configuração experimental. Use
o arquivo com `run_id` para examinar uma tentativa específica. O report inclui
sucesso, avaliação, duração, chamadas e iterações do agente, métricas de tokens
disponíveis, classificação de falhas e caminhos dos artefatos.

Para exportar resultados brutos já salvos em `results/`:

```powershell
benchmark report --input results --format json
benchmark report --input results --format csv
benchmark report --input results --format html
```

O caminho padrão de saída é `reports/report.<formato>`. Para escolher outro
caminho, informe `--output`:

```powershell
benchmark report --input results --format csv --output reports\comparacao.csv
```

Opções de `benchmark report`: `--input` escolhe a pasta que contém execuções
(padrão `results`), `--format` aceita `json`, `csv` ou `html` (padrão `json`) e
`--output` define o arquivo de saída.

### Arquivos de uma execução

```text
results/<run_id>/run.json       registro de métricas, controles e avaliação
results/<run_id>/events.jsonl  chamadas/respostas e eventos da trajetória
results/<run_id>/patch.diff    patch produzido pelo agente
results/<run_id>/...           saídas e logs dos comandos de avaliação
```

`run.json` é o resultado estruturado da tentativa; `final_response.txt`, quando
presente, contém a resposta final textual do agente. Relatórios agregados não
substituem esses dados brutos. Campos de telemetria indisponíveis ficam `null`.

## Desenvolvimento

Instale o projeto em modo editável (consulte [Instalação](#instalação)) e rode a
suíte de testes:

```powershell
python -m unittest discover -s tests -v
```

## Arquitetura, protocolo e limitações

- [Arquitetura e schemas](docs/architecture.md)
- [Metodologia e ameaças à validade](docs/methodology.md)

As tarefas devem fixar commits imutáveis. O agente trabalha em worktree
descartável; hidden tests são copiados apenas para um workspace de avaliação
separado após o término do agente. Worktrees isolam estado Git, mas não são uma
fronteira de segurança para comandos do sistema: para código não confiável, use
um container ou sandbox descartável sem segredos e com acesso limitado ao
repositório.
