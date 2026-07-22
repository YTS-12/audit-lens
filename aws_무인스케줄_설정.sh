#!/bin/bash
# ══════════════════════════════════════════════════════════════════
# 감사렌즈 완전 무인 스케줄 설정 (수정본 v2 — 재실행 안전)
# 사용법: AWS 콘솔(관리자) → CloudShell(>_) → 전체 붙여넣기 → Enter
# 효과: 평일 09:00 KST 자동 시작 + 18:10 자동 종료(백업). 주말은 수동 유지.
# ══════════════════════════════════════════════════════════════════
export AWS_PAGER=""   # ★핵심: 결과를 뷰어(less)로 열지 않게 함 — 이전 실행 실패의 원인
set +e                # 일부 단계가 이미 되어 있어도 끝까지 진행

R=ap-northeast-2
ACC=<AWS_ACCOUNT_ID>
INST=<EC2_INSTANCE_ID>
ROLE=audit-lens-scheduler-role

# ① 알람시계용 권한 명찰(IAM 역할) — 이미 있으면 건너뜀
cat > /tmp/trust.json <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF
aws iam get-role --role-name $ROLE >/dev/null 2>&1 \
  && echo "[1/4] 역할 이미 있음 - 건너뜀" \
  || { aws iam create-role --role-name $ROLE --assume-role-policy-document file:///tmp/trust.json >/dev/null && echo "[1/4] 역할 생성 완료"; }

cat > /tmp/perm.json <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["ec2:StartInstances","ec2:StopInstances"],"Resource":"arn:aws:ec2:ap-northeast-2:<AWS_ACCOUNT_ID>:instance/<EC2_INSTANCE_ID>"}]}
EOF
aws iam put-role-policy --role-name $ROLE --policy-name ec2-startstop --policy-document file:///tmp/perm.json \
  && echo "[2/4] 권한 부여 완료"

echo "역할 전파 대기 10초..." && sleep 10

# ② 평일 09:00 KST 자동 시작 — 이미 있으면 건너뜀
aws scheduler get-schedule --region $R --name audit-lens-start-0900 >/dev/null 2>&1 \
  && echo "[3/4] 시작 스케줄 이미 있음 - 건너뜀" \
  || { aws scheduler create-schedule --region $R --name audit-lens-start-0900 \
       --schedule-expression "cron(0 9 ? * MON-FRI *)" --schedule-expression-timezone "Asia/Seoul" \
       --flexible-time-window Mode=OFF \
       --target "{\"Arn\":\"arn:aws:scheduler:::aws-sdk:ec2:startInstances\",\"RoleArn\":\"arn:aws:iam::$ACC:role/$ROLE\",\"Input\":\"{\\\"InstanceIds\\\":[\\\"$INST\\\"]}\"}" >/dev/null \
       && echo "[3/4] 평일 09:00 시작 스케줄 등록 완료"; }

# ③ 평일 18:10 KST 자동 종료(서버 내부 18:00 크론의 백업) — 이미 있으면 건너뜀
aws scheduler get-schedule --region $R --name audit-lens-stop-1810 >/dev/null 2>&1 \
  && echo "[4/4] 종료 스케줄 이미 있음 - 건너뜀" \
  || { aws scheduler create-schedule --region $R --name audit-lens-stop-1810 \
       --schedule-expression "cron(10 18 ? * MON-FRI *)" --schedule-expression-timezone "Asia/Seoul" \
       --flexible-time-window Mode=OFF \
       --target "{\"Arn\":\"arn:aws:scheduler:::aws-sdk:ec2:stopInstances\",\"RoleArn\":\"arn:aws:iam::$ACC:role/$ROLE\",\"Input\":\"{\\\"InstanceIds\\\":[\\\"$INST\\\"]}\"}" >/dev/null \
       && echo "[4/4] 평일 18:10 종료 스케줄 등록 완료"; }

# ④ 최종 확인 — 아래에 스케줄 2개가 보이면 성공
echo "" && echo "===== 등록된 스케줄 목록 ====="
aws scheduler list-schedules --region $R --query "Schedules[].[Name,State]" --output table
echo "위 표에 audit-lens-start-0900 / audit-lens-stop-1810 두 줄이 ENABLED로 보이면 성공입니다."
