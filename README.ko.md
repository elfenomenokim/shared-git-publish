<div align="center">

# shared-git-publish

공유 Git 체크아웃에서 지정한 작업을 푸시하고, 로컬 변경을 보존하며 체크아웃을 갱신합니다.

[English](README.md) · Python 3.9+

[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

[설치](#설치) · [빠른 시작](#빠른-시작) · [명령](#명령) · [제한](#제한)

</div>

## 설치

소스 디렉터리에서 `pipx install .` 또는 `uv tool install .`을 실행합니다.
가상 환경을 이미 활성화했다면 `python -m pip install .`로 설치합니다.

Git 2.38 이상과 Python 3.9 이상이 필요합니다. POSIX 파일 잠금을 사용합니다.
오래된 인덱스 잠금에 소유 프로세스가 있는지 확인하려면 `lsof`를 설치하세요.

## 빠른 시작

임시 체크아웃 `work`에서 `note.txt`를 커밋하고 `origin/main`에 푸시한 다음
파일을 수정합니다. 아래 예시는 실제 출력에서 상태만 골라 표시합니다.
전체 JSON에는 커밋 ID와 복구 정보도 들어 있습니다.

```console
$ shared-git-publish publish --repo work --path note.txt --message 'Update note' | jq -c '[.publication.status, .adoption.status]'
["published","adopted"]
$ shared-git-publish adopt --repo work --target origin/main | jq -c '.adoption.status'
"up_to_date"
$ shared-git-publish publish-commit --repo work --tip HEAD --path note.txt --no-adopt | jq -c '[.publication.status, .adoption.status]'
["up_to_date","skipped"]
```

작업자마다 인덱스가 필요하면 같은 저장소에 Git worktree를 만들 수 있습니다.
Jujutsu는 작업 복사본을 커밋으로 기록하고 작업 공간을 나눕니다. GitButler는
여러 브랜치를 병렬이나 스택으로 관리합니다. 이 도구는 Git 인덱스와 원격 저장소를
그대로 쓰는 공유 체크아웃에서 지정한 작업을 푸시합니다. [Git worktree](https://git-scm.com/docs/git-worktree) · [Jujutsu 작업 복사본](https://docs.jj-vcs.dev/latest/working-copy/) · [GitButler](https://gitbutler.com/)

## 명령

| 명령 | 동작 |
| --- | --- |
| `publish --repo R --path P --message M` | 별도 인덱스로 원격 브랜치 최신 커밋 위에 새 커밋을 만듭니다. 파일별로 `--path`를 지정합니다. |
| `publish-commit --repo R --tip OID --path P` | 지정한 커밋까지의 이력을 푸시합니다. 작업 경로마다 `--path`를 지정하고, 실행 전에 전송할 이력 전체를 확인하세요. |
| `adopt --repo R --target OID` | 현재 브랜치를 로컬에서 찾을 수 있는 커밋으로 갱신합니다. 같은 패치로 이뤄진 이력은 정렬할 수 있고, 원격에 없는 로컬 커밋이 있으면 건너뜁니다. |

푸시 명령의 기본값은 `--remote origin --branch main`입니다. 체크아웃을 그대로 두려면
`--no-adopt`를 지정합니다. 작업 파일을 푸시할 때 `--base`로 편집을 시작한 커밋을,
`--source-root`로 확인을 마친 파일 트리를 전달할 수 있습니다.
원격에 기준 버전이 그대로 남아 있는 파일은 `--delete P`로 삭제할 수 있습니다.
내용이 있던 파일을 의도적으로 비우려면 `--allow-empty`를 사용합니다.

JSON 스키마 버전 1은 `publication.status`와 `adoption.status`를 따로 기록합니다.
푸시가 성공한 뒤에도 로컬 체크아웃은 갱신을 거절할 수 있습니다. 푸시 명령은 성공하거나
변경이 없으면 0으로 종료합니다. `adopt`만 실행하면 갱신 완료 또는 최신 상태는 0,
거절은 2, 건너뜀은 3으로 종료합니다. 푸시 결과가 불확실하면 원격을 확인한 다음 다시 시도하세요.

체크아웃을 갱신하기 전에 인덱스 복사본에서 동작을 확인합니다. 보존할 스테이징 상태가 있으면
`refs/shared-git-publish/backup/` 아래에 백업 ref를 만듭니다. 자동으로 만든 백업 쌍의
보존 기간은 7일입니다. 처리 중단 시 복구할 데이터는 다음에 안전하게 정리할 수 있을 때까지 남깁니다.

## 제한

한 파일의 스테이징되지 않은 변경을 작업자별로 구분할 수 없습니다. 편집 내용이 겹치면
검토한 파일 트리를 `--source-root`로 넘기세요. 작업 파일 푸시는 일반 파일을 지원하며
실행 권한을 보존합니다. 겹치는 변경, 진행 중인 Git 작업, 인덱스 잠금 때문에 갱신이
거절될 수 있습니다. macOS에서 실행을 확인했고, CI는 macOS와 Linux에서 테스트하도록 설정했습니다.

기여할 때는 임시 Git 저장소를 사용하는 테스트를 실행하세요. [기여 방법](CONTRIBUTING.md) · [MIT 라이선스](LICENSE)
