import { ComponentFixture, TestBed } from '@angular/core/testing';

import { ConversionId } from './conversion-id';

describe('ConversionId', () => {
  let component: ConversionId;
  let fixture: ComponentFixture<ConversionId>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ConversionId],
    }).compileComponents();

    fixture = TestBed.createComponent(ConversionId);
    component = fixture.componentInstance;
    await fixture.whenStable();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
