/*
 * Copyright (c) 2008-2016, Hazelcast, Inc. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.hazelcast.simulator.tests.map;

import com.hazelcast.core.Pipelining;
import com.hazelcast.map.IMap;
import com.hazelcast.simulator.probes.LatencyProbe;
import com.hazelcast.simulator.test.annotations.StartNanos;
import com.hazelcast.simulator.test.annotations.TimeStep;

import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executor;

public class LongByteArrayMapTest extends AbstractLongByteArrayMapTest {

    private final Executor callerRuns = Runnable::run;

    @TimeStep(prob = -1)
    public byte[] get(ThreadState state) {
        return getRandomMap().get(state.fixedKeyOrRandom());
    }

    @TimeStep(prob = 0)
    public Map<Long, byte[]> getAll(ThreadState state) {
        Set<Long> keys = new HashSet<>();
        for (int k = 0; k < getAllSize; k++) {
            keys.add(state.randomKey());
        }
        return getRandomMap().getAll(keys);
    }

    @TimeStep(prob = 0)
    public CompletableFuture getAsync(ThreadState state) {
        return getRandomMap().getAsync(state.randomKey()).toCompletableFuture();
    }

    @TimeStep(prob = 0.1)
    public byte[] put(ThreadState state) {
        return getRandomMap().put(state.randomKey(), state.randomValue());
    }

    /**
     * Ensures that the key does not exist before adding it again.
     */
    @TimeStep(prob = 0)
    public void deleteAndPut(ThreadState state) {
        var map = getRandomMap();
        long key = state.randomKey();
        map.delete(key);
        map.put(key, state.randomValue());
    }

    @TimeStep(prob = 0.0)
    public CompletableFuture putAsync(ThreadState state) {
        return getRandomMap().putAsync(state.randomKey(), state.randomValue()).toCompletableFuture();
    }

    @TimeStep(prob = 0)
    public void set(ThreadState state) {
        getRandomMap().set(state.randomKey(), state.randomValue());
    }

    @TimeStep(prob = 0)
    public CompletableFuture setAsync(ThreadState state) {
        return getRandomMap().setAsync(state.randomKey(), state.randomValue()).toCompletableFuture();
    }

    /**
     * Logs size of the map during test. Useful as sanity check when IMap can change size, eg. with TTL or eviction.
     */
    @TimeStep(prob = 0)
    public void sizeLog() {
        IMap<Long, byte[]> map = getRandomMap();
        logger.info("current size of {}: {}", map.getName(), map.size());
    }

    @TimeStep(prob = 0)
    public void updateAllUsingEntryProcessor() {
        IMap<Long, byte[]> map = getRandomMap();
        map.executeOnEntries(new UpdateEntryProcessor((byte) 1));
    }

    @TimeStep(prob = 0)
    public void pipelinedGet(final ThreadState state, @StartNanos final long startNanos, final LatencyProbe probe) throws Exception {
        if (state.pipeline == null) {
            state.pipeline = new Pipelining<>(pipelineDepth);
        }

        CompletableFuture<byte[]> f = getRandomMap().getAsync(state.randomKey()).toCompletableFuture();
        f.whenCompleteAsync((bytes, throwable) -> probe.done(startNanos), callerRuns);
        state.pipeline.add(f);
        state.i++;
        if (state.i == pipelineIterations) {
            state.i = 0;
            state.pipeline.results();
            state.pipeline = null;
        }
    }

}
