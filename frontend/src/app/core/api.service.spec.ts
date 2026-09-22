import { parseSseFrame } from './api.service';

describe('parseSseFrame', () => {
  it('reads the event name and data payload', () => {
    expect(parseSseFrame('event: message\ndata: {"content": "hi"}')).toEqual({
      event: 'message',
      data: '{"content": "hi"}',
    });
  });

  it('defaults to the message event when only data is sent', () => {
    expect(parseSseFrame('data: hello')).toEqual({ event: 'message', data: 'hello' });
  });

  it('tolerates CRLF line endings', () => {
    expect(parseSseFrame('event: done\r\ndata: {}')).toEqual({ event: 'done', data: '{}' });
  });

  it('joins multi-line data the way the SSE spec requires', () => {
    expect(parseSseFrame('data: one\ndata: two')).toEqual({ event: 'message', data: 'one\ntwo' });
  });

  it('ignores comment lines and blank frames', () => {
    expect(parseSseFrame(': keep-alive')).toBeNull();
    expect(parseSseFrame('')).toBeNull();
  });
});
