# FedOps Chart

환경별 설치 절차와 검증 범위는 [저장소 README](../../README.md)를 참고함.

`deployment.provider`는 환경 구분이며 클라우드 인프라를 생성하지 않음.
`deployment.topology=single-node`는 지정 노드 배치, `multi-node`는 분산 가능한 배치임.
기본 카카오클라우드 구성은 `profiles/kakaocloud-local.yaml`을 사용하며 CSI 전환이 필요하지 않음.

## 외부 Secret 계약

| 기본 Secret 이름 | 필요한 키 |
|---|---|
| fedops-web | MONGO_URI, JWT_SECRET |
| fedops-performance | MONGODB_URI |
| fedops-registry | MONGODB_URI, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY |
| fedops-mongo | MONGO_INITDB_ROOT_USERNAME, MONGO_INITDB_ROOT_PASSWORD |
| fedops-registry-mongo | MONGO_INITDB_ROOT_USERNAME, MONGO_INITDB_ROOT_PASSWORD |
| fedops-minio | MINIO_ROOT_USER, MINIO_ROOT_PASSWORD |
| fedops-task-storage | ACCESS_KEY_ID, ACCESS_SECRET_KEY, BUCKET_NAME |
| fedops-smtp | SPRING_MAIL_USERNAME, SPRING_MAIL_PASSWORD |

Secret은 Chart에 포함하지 않음. 신규 설치의 기본 이름 구성에서는 저장소의 bootstrap 스크립트를 사용할 수 있음. SMTP는 별도 준비가 필요함.

기존 F 환경 예시는 참고용이며 완성된 설치 설정이 아님. 현재 권장 명령은 저장소 루트 README와 profiles/sites를 사용함. `tests/static-values.yaml`은 정적 검사 전용으로 실제 배포하지 않음.
