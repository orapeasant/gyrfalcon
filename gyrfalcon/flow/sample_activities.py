"""Five small activities for the dashboard graph designer example."""

from gyrfalcon.flow.templates import activity


@activity(name="hello1", version="1")
def hello1(name: str = "world") -> dict:
    return {"message": f"hello1 {name}"}


@activity(name="hello2", version="1")
def hello2(previous: dict) -> dict:
    return {"message": f"{previous['message']} → hello2"}


@activity(name="hello3", version="1")
def hello3(previous: dict) -> dict:
    return {"message": f"{previous['message']} → hello3"}


@activity(name="hello4", version="1")
def hello4(previous: dict) -> dict:
    return {"message": f"{previous['message']} → hello4"}


@activity(name="hello5", version="1")
def hello5(previous: dict) -> dict:
    return {"message": f"{previous['message']} → hello5"}


def sample_graph() -> dict:
    nodes = [
        {"id": "hello1", "type": "python", "activity": "hello1", "version": "1",
         "inputs": {"name": {"ref": "inputs.name"}}},
    ]
    for number in range(2, 6):
        nodes.append({
            "id": f"hello{number}", "type": "python",
            "activity": f"hello{number}", "version": "1",
            "inputs": {"previous": {"ref": f"hello{number - 1}"}},
        })
    return {
        "nodes": nodes,
        "edges": [
            {"from": f"hello{number}", "to": f"hello{number + 1}"}
            for number in range(1, 5)
        ],
    }
