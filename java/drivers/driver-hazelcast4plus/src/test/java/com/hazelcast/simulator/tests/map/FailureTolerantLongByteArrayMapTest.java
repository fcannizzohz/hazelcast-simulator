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
import com.hazelcast.map.IMap;
import com.hazelcast.simulator.common.TestCase;
import com.hazelcast.simulator.hazelcast4plus.HazelcastInstances;
import com.hazelcast.simulator.hz.HazelcastTest;
import com.hazelcast.simulator.test.annotations.TimeStep;
import com.hazelcast.simulator.worker.testcontainer.PropertyBinding;
import com.hazelcast.simulator.worker.testcontainer.TimeStepModel;
import com.hazelcast.splitbrainprotection.SplitBrainProtectionException;
import com.hazelcast.spi.exception.TargetDisconnectedException;
import org.junit.Before;
import org.junit.Test;
import org.mockito.ArgumentCaptor;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.List;
import java.util.concurrent.CompletableFuture;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertSame;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.Mockito.doNothing;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

public class FailureTolerantLongByteArrayMapTest {

    private TestSubject test;
    private IMap<Long, byte[]> map;
    private LongByteArrayMapTest.ThreadState state;

    @Before
    @SuppressWarnings("unchecked")
    public void setUp() throws ReflectiveOperationException {
        HazelcastInstance instance = mock(HazelcastInstance.class);
        map = mock(IMap.class);
        when(instance.<Long, byte[]>getMap("map")).thenReturn(map);
        when(map.size()).thenReturn(1);

        test = new TestSubject();
        test.name = "map";
        test.keyDomain = 1;
        test.valueCount = 1;
        test.minValueLength = 1;
        test.maxValueLength = 1;
        test.setTargetInstance(instance);
        test.setUp();
        state = test.new ThreadState();
    }

    @Test
    public void getRetriesSplitBrainProtectionFailure() {
        byte[] value = {42};
        when(map.get(0L))
                .thenThrow(new SplitBrainProtectionException("expected"))
                .thenReturn(value);

        test.get(state);
        awaitInvocations(() -> verify(map, times(2)).get(0L));
        test.verifyFailureRecovery();
    }

    @Test
    public void setRetriesSameValueAfterTargetDisconnection() {
        doThrow(new TargetDisconnectedException("expected"))
                .doNothing()
                .when(map).set(anyLong(), any(byte[].class));

        test.set(state);

        awaitInvocations(() -> verify(map, times(2)).set(anyLong(), any(byte[].class)));

        ArgumentCaptor<Long> keyCaptor = ArgumentCaptor.forClass(Long.class);
        ArgumentCaptor<byte[]> valueCaptor = ArgumentCaptor.forClass(byte[].class);
        verify(map, times(2)).set(keyCaptor.capture(), valueCaptor.capture());
        assertEquals(keyCaptor.getAllValues().get(0), keyCaptor.getAllValues().get(1));
        assertSame(valueCaptor.getAllValues().get(0), valueCaptor.getAllValues().get(1));
        test.verifyFailureRecovery();
    }

    @Test
    public void asynchronousGetRetriesExpectedFailure() {
        byte[] value = {42};
        when(map.getAsync(0L))
                .thenReturn(CompletableFuture.failedFuture(new TargetDisconnectedException("expected")))
                .thenReturn(CompletableFuture.completedFuture(value));

        assertArrayEquals(value, test.getAsync(state).join());
        awaitInvocations(() -> verify(map, times(2)).getAsync(0L));
        test.verifyFailureRecovery();
    }

    @Test
    public void overridesEveryMapTimeStep() throws NoSuchMethodException {
        for (Method method : LongByteArrayMapTest.class.getDeclaredMethods()) {
            if (method.isAnnotationPresent(TimeStep.class)) {
                FailureTolerantLongByteArrayMap.class.getDeclaredMethod(method.getName(), method.getParameterTypes());
            }
        }
    }

    @Test
    public void simulatorCanConstructInheritedThreadState() throws Exception {
        TestCase testCase = new TestCase("id");
        TimeStepModel model = new TimeStepModel(FailureTolerantLongByteArrayMap.class,
                new PropertyBinding(testCase));

        assertEquals(LongByteArrayMapTest.class,
                model.getThreadStateConstructor("").getParameterTypes()[0]);
        model.getThreadStateConstructor("").newInstance(test);
    }

    @Test(expected = IllegalStateException.class)
    public void unexpectedExceptionRemainsFatal() {
        when(map.get(0L)).thenThrow(new IllegalStateException("unexpected"));
        test.get(state);
    }

    @Test(expected = AssertionError.class)
    public void missingSuccessfulValueFailsCorrectnessCheck() {
        when(map.get(0L)).thenReturn(null);
        test.get(state);
    }

    private static void awaitInvocations(Runnable assertion) {
        AssertionError last = null;
        for (int i = 0; i < 100; i++) {
            try {
                assertion.run();
                return;
            } catch (AssertionError failure) {
                last = failure;
                try {
                    Thread.sleep(10);
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    throw failure;
                }
            }
        }
        throw last;
    }

    private static final class TestSubject extends FailureTolerantLongByteArrayMap {

        private void setTargetInstance(HazelcastInstance instance) throws ReflectiveOperationException {
            targetInstance = instance;
            Field field = HazelcastTest.class.getDeclaredField("targetInstances");
            field.setAccessible(true);
            field.set(this, new HazelcastInstances(List.of(instance)));
        }

        @Override
        protected long retryPauseMillis() {
            return 0;
        }

        @Override
        protected long minimumRecoveryMillis() {
            return 0;
        }
    }
}
