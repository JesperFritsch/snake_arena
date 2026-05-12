Build service:
    - needs to build user code images
    - outputs a tagged image thats easy to relate to a user

Match runner:
    Pull/verify N submission images exist
    Create an isolated network
    Start each agent with sandbox flags and resource limits
    Wait for agents to be gRPC-ready (with timeout)
    Start the sim with the right --external-snake-targets
    Stream sim output for logging
    Wait for the sim to finish (with hard wall-clock timeout)
    Collect the replay file from a mounted volume
    Tear down: stop containers, remove network
    Return: result (winner, scores, replay path, per-agent stats, crash flags)

    Failures:
        Agent fails to start (image broken, gRPC never opens)
        Agent crashes mid-match
        Agent hangs (no response, breaching wall-clock budget)
        Sim crashes
        Out-of-memory
        Match exceeds total wall-clock limit
        gVisor escape attempt (logged, treated as crash)



Scheduler:
    - decides what matches to run and which competitors in each match

State store:
    - tracks users, their code and their agents results


store:
    schema
        users            (id, email, ...)
        projects         (id, user_id, name, language, created_at)
        code_versions    (id, project_id, version_num, code_blob, created_at)
        submissions      (id, project_id, code_version_id, image_tag, status, created_at)
                        -- status: building | ready | failed | gc'd
        matches          (id, kind, status, created_at, started_at, finished_at)
                        -- kind: test | tournament
        match_agents     (match_id, slot, submission_id, result, stats_json)
        replays          (match_id, blob_ref)
        tournaments      (id, name, scheduled_at, status)
        tournament_matches (tournament_id, match_id)