import { ComponentFixture, TestBed } from '@angular/core/testing';

import { ConvCondition } from './conv-condition';

describe('ConvCondition', () => {
  let component: ConvCondition;
  let fixture: ComponentFixture<ConvCondition>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ConvCondition],
    }).compileComponents();

    fixture = TestBed.createComponent(ConvCondition);
    component = fixture.componentInstance;
    await fixture.whenStable();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
