import assert from 'node:assert/strict';
import test from 'node:test';

class FakeSocket {
  constructor(id, role = 'remote') {
    this.id = id;
    this.handshake = { query: { role }, address: '127.0.0.1', headers: {} };
    this.handlers = new Map();
    this.received = [];
    this.broadcastEmitted = [];
    this.broadcast = {
      emit: (event, data) => {
        this.broadcastEmitted.push({ event, data });
      },
    };
  }

  on(event, handler) {
    this.handlers.set(event, handler);
  }

  emit(event, data) {
    this.received.push({ event, data });
  }

  trigger(event, data) {
    return this.handlers.get(event)?.(data);
  }

  last(event) {
    return this.received.filter(item => item.event === event).at(-1)?.data;
  }
}

// Token-bucket test
function createRateLimiter() {
  const socketRateLimits = new Map();
  return (socketId, action, limitPerSec) => {
    const now = Date.now();
    let entry = socketRateLimits.get(socketId);
    if (!entry) {
      entry = {};
      socketRateLimits.set(socketId, entry);
    }
    const bucket = entry[action] || { tokens: limitPerSec, last: now };
    const elapsed = (now - bucket.last) / 1000;
    bucket.tokens = Math.min(limitPerSec, bucket.tokens + elapsed * limitPerSec);
    bucket.last = now;
    if (bucket.tokens >= 1) {
      bucket.tokens -= 1;
      entry[action] = bucket;
      return true;
    }
    return false;
  };
}

test('Token bucket rate limiter throttles high frequency bursts', () => {
  const checkRateLimit = createRateLimiter();
  const socketId = 'sock-123';

  // 3 tokens per second
  assert.equal(checkRateLimit(socketId, 'danmaku', 3), true);
  assert.equal(checkRateLimit(socketId, 'danmaku', 3), true);
  assert.equal(checkRateLimit(socketId, 'danmaku', 3), true);
  // 4th immediate request should be throttled
  assert.equal(checkRateLimit(socketId, 'danmaku', 3), false);
});

test('Queue item movement logic respects bounds and order', () => {
  const queue = [
    { queueId: 'playing', title: 'Song 0' },
    { queueId: 'q1', title: 'Song 1' },
    { queueId: 'q2', title: 'Song 2' },
    { queueId: 'q3', title: 'Song 3' },
  ];

  const moveQueueItem = ({ queueId, direction }) => {
    const index = queue.findIndex(item => item.queueId === queueId);
    if (index <= 0) return; // Cannot move current playing song
    if (direction === 'up' && index > 1) {
      [queue[index - 1], queue[index]] = [queue[index], queue[index - 1]];
    } else if (direction === 'down' && index < queue.length - 1) {
      [queue[index], queue[index + 1]] = [queue[index + 1], queue[index]];
    }
  };

  // Move q2 up -> swaps with q1
  moveQueueItem({ queueId: 'q2', direction: 'up' });
  assert.equal(queue[1].queueId, 'q2');
  assert.equal(queue[2].queueId, 'q1');

  // Attempting to move index 1 up should not displace currently playing song (index 0)
  moveQueueItem({ queueId: 'q2', direction: 'up' });
  assert.equal(queue[1].queueId, 'q2');

  // Move q2 down -> swaps back with q1
  moveQueueItem({ queueId: 'q2', direction: 'down' });
  assert.equal(queue[1].queueId, 'q1');
  assert.equal(queue[2].queueId, 'q2');

  // Move q3 down -> already at end, should stay at end
  moveQueueItem({ queueId: 'q3', direction: 'down' });
  assert.equal(queue[3].queueId, 'q3');
});

test('Pitch data and score report protocol payload structure', () => {
  const socket = new FakeSocket('sock-singer', 'remote');

  // Wire mock pitch_data handler
  socket.on('pitch_data', payload => {
    if (payload && typeof payload === 'object') {
      socket.broadcast.emit('remote_pitch', {
        ...payload,
        serverTime: Date.now(),
      });
    }
  });

  socket.trigger('pitch_data', { midi: 60.5, hz: 261.63, vol: 0.12, user: '小明' });
  assert.equal(socket.broadcastEmitted.length, 1);
  assert.equal(socket.broadcastEmitted[0].event, 'remote_pitch');
  assert.equal(socket.broadcastEmitted[0].data.midi, 60.5);
  assert.equal(socket.broadcastEmitted[0].data.user, '小明');

  // Wire mock report_score handler
  socket.on('report_score', payload => {
    socket.broadcast.emit('score_updated', payload);
  });

  socket.trigger('report_score', { song: '青花瓷', score: 94.2, grade: 'SS', user: '小明' });
  assert.equal(socket.broadcastEmitted.length, 2);
  assert.equal(socket.broadcastEmitted[1].event, 'score_updated');
  assert.equal(socket.broadcastEmitted[1].data.score, 94.2);
});
