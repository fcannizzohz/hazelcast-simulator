/*
 * Copyright (c) 2008-2026, Hazelcast, Inc. All Rights Reserved.
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

import com.hazelcast.core.HazelcastInstance;
import com.hazelcast.core.Pipelining;
import com.hazelcast.map.EntryProcessor;
import com.hazelcast.map.IMap;
import com.hazelcast.simulator.hz.HazelcastTest;
import com.hazelcast.simulator.test.BaseThreadState;
import com.hazelcast.simulator.test.annotations.Prepare;
import com.hazelcast.simulator.test.annotations.Setup;
import com.hazelcast.simulator.test.annotations.Teardown;
import com.hazelcast.simulator.worker.loadsupport.Streamer;
import com.hazelcast.simulator.worker.loadsupport.StreamerFactory;

import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.concurrent.ConcurrentHashMap;

import static com.hazelcast.simulator.tests.helpers.HazelcastTestUtils.assignKeyToIndex;
import static com.hazelcast.simulator.utils.GeneratorUtils.generateByteArrays;
import static java.lang.Thread.currentThread;

/** Shared configuration and lifecycle for long-key, byte-array map workloads. */
public abstract class AbstractLongByteArrayMapTest extends HazelcastTest {

    public int keyDomain = 10000;
    public int valueCount = 10000;
    public int minValueLength = 10;
    public int maxValueLength = 10;
    public int pipelineDepth = 10;
    public int pipelineIterations = 100;
    public int getAllSize = 5;
    public int mapCount = 1;
    /** The fixed keys used by get operations. Zero means all keys are random. */
    public int fixedKeyDomain = 0;
    /** Percentage probability of selecting a key from {@link #fixedKeyDomain}. */
    public int fixedKeyProbability = 0;

    private byte[][] values;
    private final List<List<IMap<Long, byte[]>>> maps = new ArrayList<>();
    private final Random random = new Random();
    private final Map<Thread, Integer> clientIndexForThread = new ConcurrentHashMap<>();

    @Setup
    public void setUp() {
        for (HazelcastInstance instance : getTargetInstances()) {
            List<IMap<Long, byte[]>> mapsForInstance = new ArrayList<>();
            maps.add(mapsForInstance);
            for (int i = 0; i < mapCount; i++) {
                mapsForInstance.add(instance.getMap(mapName(i)));
            }
        }
        values = generateByteArrays(valueCount, minValueLength, maxValueLength);
    }

    @Prepare(global = true)
    public void prepare() {
        // One instance is enough to prepare every distributed map.
        for (IMap<Long, byte[]> map : maps.get(0)) {
            Streamer<Long, byte[]> streamer = StreamerFactory.getInstance(map);
            for (long key = 0; key < keyDomain; key++) {
                streamer.pushEntry(key, values[random.nextInt(valueCount)]);
            }
            streamer.await();
        }
    }

    protected final IMap<Long, byte[]> getRandomMap() {
        List<IMap<Long, byte[]>> mapsToSelectFrom;
        if (maps.size() == 1) {
            mapsToSelectFrom = maps.get(0);
        } else {
            Integer clientIndex = clientIndexForThread.get(currentThread());
            mapsToSelectFrom = maps.get(clientIndex == null ? putClientForCurrentThread() : clientIndex);
        }
        return mapsToSelectFrom.get(random.nextInt(mapCount));
    }

    protected final void verifyValue(byte[] value, String operation) {
        if (value == null || value.length < minValueLength || value.length > maxValueLength) {
            throw new AssertionError(operation + " returned an invalid value");
        }
    }

    protected final void verifyGetAll(Map<Long, byte[]> result, int expectedSize) {
        if (result.size() != expectedSize) {
            throw new AssertionError("getAll returned " + result.size() + " entries; expected " + expectedSize);
        }
        result.values().forEach(value -> verifyValue(value, "getAll"));
    }

    protected final void verifyMapSizes() {
        for (int mapIndex = 0; mapIndex < mapCount; mapIndex++) {
            String mapName = mapName(mapIndex);
            int actualSize = targetInstance.getMap(mapName).size();
            if (actualSize != keyDomain) {
                throw new AssertionError("Map [" + mapName + "] contains " + actualSize
                        + " entries; expected " + keyDomain);
            }
        }
    }

    private String mapName(int index) {
        return mapCount == 1 ? name : name + "_" + index;
    }

    private synchronized int putClientForCurrentThread() {
        return assignKeyToIndex(getTargetInstances().size(), currentThread(), clientIndexForThread);
    }

    public class ThreadState extends BaseThreadState {
        public static final int HIGHEST_PROBABILITY = 100;
        protected Pipelining<byte[]> pipeline;
        protected int i;

        protected long fixedKeyOrRandom() {
            if (fixedKeyDomain > 0 && fixedKeyDomain < keyDomain && fixedKeyProbability > 0
                    && randomInt(HIGHEST_PROBABILITY) < fixedKeyProbability) {
                return randomLong(fixedKeyDomain);
            }
            return randomKey();
        }

        protected long randomKey() {
            return randomLong(keyDomain);
        }

        protected byte[] randomValue() {
            return values[randomInt(values.length)];
        }
    }

    protected static final class UpdateEntryProcessor implements EntryProcessor<Long, byte[], Object> {
        private final byte increment;

        protected UpdateEntryProcessor(byte increment) {
            this.increment = increment;
        }

        @Override
        public Object process(Map.Entry<Long, byte[]> entry) {
            byte[] value = entry.getValue();
            value[0] += increment;
            entry.setValue(value);
            return null;
        }
    }

    @Teardown
    public void tearDown() {
        maps.stream().flatMap(Collection::stream).forEach(IMap::destroy);
    }
}
