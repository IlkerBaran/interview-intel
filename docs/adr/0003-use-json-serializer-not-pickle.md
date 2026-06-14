# 0003. Use JSON Task Serializer, Not Pickle

## Status

Accepted

## Context

Celery needs to send task arguments and task results through Redis. To do that, it has to convert the data 
into a format that can be stored and sent. This is called serialization.

The two options considered here are JSON and pickle.

Pickle is convenient because it can serialize many Python objects. The problem is that pickle can also be unsafe:
when the worker reads a pickled message, it can deserialize Python objects. If a bad payload ever reached the broker,
that could create a security risk.

JSON is safer and simpler for this project. It only supports plain data types like strings, numbers, lists,
and dictionaries.

This choice should be made early, before real tasks depend on the queue. Changing serializers later would be
more painful.

## Decision

Celery will use JSON for task arguments and results:

```python
task_serializer = "json"
result_serializer = "json"
accept_content = ["json"]
```

Pickle will not be accepted.

## Consequences

* Tasks must receive simple JSON-serializable data, such as IDs, strings, numbers, lists, or dictionaries.
* Tasks should not receive SQLAlchemy model objects or other Python objects directly.
* This supports the rule: pass IDs to tasks, then re-fetch the database object inside the task.
* This avoids the security risk of accepting pickle messages from the broker.
* JSON does not automatically keep messages small. A large JSON payload can still be sent, so task messages 
should still be kept small by convention.
* Making this decision now is simple. Changing it later, after real tasks exist, would be harder.
