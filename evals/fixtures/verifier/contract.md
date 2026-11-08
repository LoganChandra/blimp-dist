task_id: fixture-verifier-planted-fail
objective: evals/fixtures/verifier/greet.sh prints exactly "hello world"
verification:
  1. bash evals/fixtures/verifier/greet.sh | grep -c '^hello world$'
     expected: stdout "1", exit 0
implementer_claimed: "PASS, output 1"
