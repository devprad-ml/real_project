"""GET /chat: the page is public; everything it shows comes from keyed API calls."""


def test_the_chat_page_is_served_without_a_key(client):
    r = client.get("/chat")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert 'fetch(path' in r.text
