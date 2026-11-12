# <<REDACTED 28 lines>>
    # #671: the Discord message showing this menu, so startup can retire a menu
    # the pane has since closed instead of leaving it looking clickable. None on
    # rows written before #671 (and on any path that posts no message).
    message_id: int | None = None
# <<REDACTED 17 lines>>
        message_id: int | None = None,
    # <<REDACTED 6 lines>>
                    (thread_id, session_id, questions_json, question_idx, message_id)
                VALUES (?, ?, ?, ?, ?)
                # <<REDACTED 1 lines>>
                (thread_id, session_id, json.dumps(questions), question_idx, message_id),
            # <<REDACTED 13 lines>>
                "SELECT thread_id, session_id, questions_json, question_idx, created_at, "
                "message_id FROM pending_asks WHERE thread_id = ?",
                # <<REDACTED 12 lines>>
            message_id=row[5],
        # <<REDACTED 14 lines>>
                "SELECT thread_id, session_id, questions_json, question_idx, created_at, "
                "message_id FROM pending_asks ORDER BY created_at"
            # <<REDACTED 10 lines>>
                message_id=row[5],
            # <<REDACTED 14 lines>>
